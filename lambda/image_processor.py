import base64
import io
import json
import os
import time
from decimal import Decimal
import random

import boto3
from PIL import Image
from botocore.exceptions import ClientError

# AWS clients
region = "us-west-2"
rekognition = boto3.client("rekognition", region_name=region)
bedrock = boto3.client("bedrock-runtime", region_name=region)
s3 = boto3.client("s3")
dynamodb = boto3.resource("dynamodb", region_name=region)

# Environment variables
TABLE_NAME = os.environ["TABLE_NAME"]
COLLECTION_ID = os.environ["COLLECTION_ID"]
MODEL_ID = os.environ["MODEL_ID"]


def handler(event, context):
    try:
        # Add larger random delay to spread out concurrent requests
        time.sleep(random.uniform(2, 8))
        
        bucket = event["Records"][0]["s3"]["bucket"]["name"]
        key = event["Records"][0]["s3"]["object"]["key"]
        
        # URL decode the key to handle special characters
        from urllib.parse import unquote_plus
        key = unquote_plus(key)

        print(f"Processing image: {bucket}/{key}")

        faces, labels = detect_and_recognize_faces(bucket, key)
        caption, generic_caption = generate_caption(bucket, key, faces)
        store_metadata(bucket, key, faces, labels, caption, generic_caption)
        
        # Store labels in Neptune for relationship building
        store_labels_in_neptune(labels, faces)

        return {
            "statusCode": 200,
            "body": json.dumps(
                {
                    "message": "Image processed successfully",
                    "faces": faces,
                    "caption": caption,
                    "generic_caption": generic_caption,
                    "image": f"{bucket}/{key}",
                }
            ),
        }

    except Exception as e:
        print(f"Error processing image: {str(e)}")
        return {"statusCode": 500, "body": json.dumps({"error": str(e)})}


def resize_image_to_under_5mb(image_bytes, max_dim=1024):
    image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    image.thumbnail((max_dim, max_dim))
    quality = 85

    for _ in range(8):
        buf = io.BytesIO()
        image.save(buf, format="JPEG", quality=quality, optimize=True)
        if buf.tell() <= 5 * 1024 * 1024:
            return buf.getvalue()
        quality -= 10

    print("Warning: Unable to compress under 5MB")
    return buf.getvalue()


def ensure_collection_exists():
    try:
        collections = rekognition.list_collections().get("CollectionIds", [])
        if COLLECTION_ID not in collections:
            rekognition.create_collection(CollectionId=COLLECTION_ID)
        return True
    except Exception as e:
        print(f"Error ensuring collection exists: {str(e)}")
        return False


def crop_face(image, bbox, padding_ratio=0.3):
    width, height = image.size
    left = int(bbox["Left"] * width)
    top = int(bbox["Top"] * height)
    w = int(bbox["Width"] * width)
    h = int(bbox["Height"] * height)

    pad_w = int(w * padding_ratio)
    pad_h = int(h * padding_ratio)

    x1 = max(0, left - pad_w)
    y1 = max(0, top - pad_h)
    x2 = min(width, left + w + pad_w)
    y2 = min(height, top + h + pad_h)

    return image.crop((x1, y1, x2, y2))


def detect_labels(image_bytes):
    """Detect labels in the image using Rekognition"""
    try:
        response = rekognition.detect_labels(
            Image={'Bytes': image_bytes},
            MaxLabels=20,
            MinConfidence=70
        )
        
        labels = []
        for label in response.get('Labels', []):
            labels.append({
                'name': label['Name'],
                'confidence': label['Confidence'],
                'categories': [cat['Name'] for cat in label.get('Categories', [])]
            })
        
        print(f"Detected {len(labels)} labels")
        return labels
        
    except Exception as e:
        print(f"Label detection error: {str(e)}")
        return []

def detect_and_recognize_faces(bucket, key):
    faces = []
    labels = []
    if not ensure_collection_exists():
        return faces, labels

    try:
        s3_obj = s3.get_object(Bucket=bucket, Key=key)
        original_bytes = s3_obj["Body"].read()
        image_bytes = resize_image_to_under_5mb(original_bytes)
        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        
        # Detect labels
        labels = detect_labels(image_bytes)

        rek_response = rekognition.detect_faces(
            Image={"Bytes": image_bytes}, Attributes=["ALL"]
        )

        face_details = rek_response.get("FaceDetails", [])
        print(f"Detected {len(face_details)} face(s)")

        for i, face_detail in enumerate(face_details):
            try:
                bbox = face_detail["BoundingBox"]
                area = bbox["Width"] * bbox["Height"]
                if area < 0.005:
                    print(f"Face {i+1}: too small, skipping (area={area:.5f})")
                    continue

                cropped = crop_face(image, bbox)
                buf = io.BytesIO()
                cropped.save(buf, format="JPEG")
                cropped_bytes = resize_image_to_under_5mb(buf.getvalue())

                detection_conf = face_detail["Confidence"]

                match_response = rekognition.search_faces_by_image(
                    CollectionId=COLLECTION_ID,
                    Image={"Bytes": cropped_bytes},
                    FaceMatchThreshold=80,
                    MaxFaces=1,
                )

                # Extract useful face attributes
                face_attributes = {}
                for attr in ["Gender", "AgeRange", "Smile", "Eyeglasses", "Sunglasses", "Beard", "Mustache"]:
                    if attr in face_detail:
                        face_attributes[attr] = face_detail[attr]

                if match_response.get("FaceMatches"):
                    best = match_response["FaceMatches"][0]
                    faces.append(
                        {
                            "name": best["Face"]["ExternalImageId"],
                            "confidence": f"{best['Similarity']:.2f}",
                            "detection_confidence": f"{detection_conf:.2f}",
                            "details": face_attributes
                        }
                    )
                    print(
                        f"Face {i+1}: matched {best['Face']['ExternalImageId']} ({best['Similarity']:.1f}%)"
                    )
                else:
                    print(f"Face {i+1}: no match found")
                    faces.append(
                        {
                            "name": "unknown",
                            "confidence": "0.00",
                            "detection_confidence": f"{detection_conf:.2f}",
                            "details": face_attributes
                        }
                    )

            except Exception as e:
                print(f"Face {i+1}: error — {str(e)}")

    except Exception as e:
        print(f"Face detection error: {str(e)}")

    return faces, labels


def get_relationship_context_neptune(face_names):
    """Get relationship context from Neptune"""
    if not face_names:
        return ""
    
    try:
        from gremlin_python.driver.driver_remote_connection import DriverRemoteConnection
        from gremlin_python.process.anonymous_traversal import traversal
        from gremlin_python.process.graph_traversal import __
        
        neptune_endpoint = os.environ.get("NEPTUNE_ENDPOINT", "")
        if not neptune_endpoint:
            return ""
            
        connection = DriverRemoteConnection(f'wss://{neptune_endpoint}:8182/gremlin', 'g')
        g = traversal().withRemote(connection)
        
        context_parts = []
        
        # Get individual person info (age, gender)
        for name in face_names:
            try:
                person_data = g.V().has('name', name.lower()).valueMap().next()
                if person_data:
                    age = person_data.get('age', ['unknown'])[0]
                    gender = person_data.get('gender', ['person'])[0]
                    
                    if age != 'adult':
                        context_parts.append(f"{name.title()} is a {age} year old {gender}")
                    else:
                        context_parts.append(f"{name.title()} is an adult {gender}")
            except:
                pass
        
        # Get relationship info for pairs
        if len(face_names) >= 2:
            for i, name1 in enumerate(face_names):
                for name2 in face_names[i+1:]:
                    try:
                        edges = g.V().has('name', name1.lower()).bothE().where(
                            __.otherV().has('name', name2.lower())
                        ).valueMap(True).toList()
                        
                        for edge in edges:
                            description = edge.get("description", [""])[0]
                            if description:
                                context_parts.append(description)
                    except:
                        pass
        
        connection.close()
        
        if context_parts:
            return f" Context: {'; '.join(context_parts)}."
        return ""
        
    except Exception as e:
        print(f"Error getting Neptune relationship context: {str(e)}")
        return ""

def get_relationship_context(face_names):
    """Get relationship context using Neptune"""
    return get_relationship_context_neptune(face_names)

def personalize_description(generic_caption, faces):
    if not faces:
        return generic_caption

    # Get names of recognized faces and count total faces
    face_names = [f["name"] for f in faces if f["name"] != "unknown"]
    total_faces = len(faces)
    recognized_faces = len(face_names)
    unknown_faces = total_faces - recognized_faces
    
    if not face_names:
        return generic_caption

    # Add relationship context
    relationship_context = get_relationship_context(face_names)
    
    # Create more accurate prompt based on face counts
    if recognized_faces == 1 and total_faces == 1:
        # Single person, fully recognized
        name_instruction = f"Replace 'person' or 'people' with {face_names[0]}."
    elif recognized_faces == 1 and unknown_faces > 0:
        # One recognized person plus unknown people
        name_instruction = f"Replace references to people with '{face_names[0]} and others' or '{face_names[0]} with friends'."
    elif recognized_faces > 1 and unknown_faces == 0:
        # Multiple recognized people, no unknowns
        name_instruction = f"Replace 'people' with the names: {', '.join(face_names)}."
    else:
        # Multiple recognized people plus unknowns
        name_instruction = f"Replace references to people with '{', '.join(face_names)} and others'."
    
    prompt = (
        f"{name_instruction} "
        f"Be careful not to duplicate names or assign the same name to multiple people. "
        f"DO NOT assign specific actions to specific people unless the original description is certain. "
        f"Keep actions general (e.g., 'someone holds flowers' rather than 'Kara holds flowers'). "
        f"If the description mentions specific counts (like 'two people'), adjust appropriately. "
        f"Keep all other details the same. Do not add explanations or refusals."
        f"{relationship_context}\n\n"
        f'Description: "{generic_caption}"'
    )

    try:
        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 250,
            "temperature": 0.3,  # Lower temperature for more consistent results
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": prompt}],
                }
            ],
        }

        # Add retry logic for personalization too
        max_retries = 6
        for attempt in range(max_retries):
            try:
                response = bedrock.invoke_model(modelId=MODEL_ID, body=json.dumps(body))
                result = json.loads(response["body"].read())
                return result["content"][0]["text"].strip('"').strip("'")
            except ClientError as e:
                if e.response['Error']['Code'] == 'ThrottlingException' and attempt < max_retries - 1:
                    wait_time = min(60, (3 ** attempt) + random.uniform(0, 5))
                    print(f"Bedrock throttled in personalization, waiting {wait_time:.2f}s (attempt {attempt + 1}/{max_retries})")
                    time.sleep(wait_time)
                else:
                    print(f"Personalization failed after {max_retries} retries: {str(e)}")
                    return generic_caption

    except Exception as e:
        print(f"Personalization error: {str(e)}")
        return generic_caption


def generate_caption(bucket, key, faces):
    try:
        s3_obj = s3.get_object(Bucket=bucket, Key=key)
        image_bytes = s3_obj["Body"].read()
        image_bytes = resize_image_to_under_5mb(image_bytes)
        image_b64 = base64.b64encode(image_bytes).decode("utf-8")
        
        # Extract age information from detected faces
        age_info = ""
        if faces:
            age_info = "\n\nDetected people information:\n"
            for i, face in enumerate(faces):
                if "details" in face and "AgeRange" in face["details"]:
                    age_range = face["details"]["AgeRange"]
                    age_info += f"Person {i+1}: Age range {age_range['Low']}-{age_range['High']}\n"
        
        prompt = (
            "Write a simple, factual description of this photo in 20-30 words. "
            "Focus on location and activities, NOT clothing details. "
            "DO NOT include age descriptions. "
            "DO NOT use phrases like 'I apologize' or 'I notice'. "
            "Always write in third person perspective. "
            "DO NOT assign specific actions to specific people unless absolutely certain. "
            "Use general terms like 'people are' or 'someone is' instead of assuming who does what. "
            "If there are only the recognized people and no others, just describe them directly without saying 'with others'. "
            "If there are unknown people beyond the recognized ones, say 'with others' or 'with friends', NOT 'family members'. "
            "Describe the event or activity they're doing."
        )

        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 250,
            "temperature": 0.4,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": image_b64,
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
        }

        # Add retry logic for Bedrock throttling
        max_retries = 6
        for attempt in range(max_retries):
            try:
                res = bedrock.invoke_model(modelId=MODEL_ID, body=json.dumps(body))
                generic = json.loads(res["body"].read())["content"][0]["text"].strip('"').strip("'")
                print(f"Generic caption: {generic}")
                break
            except ClientError as e:
                if e.response['Error']['Code'] == 'ThrottlingException' and attempt < max_retries - 1:
                    wait_time = min(60, (3 ** attempt) + random.uniform(0, 5))  # Exponential backoff with jitter, max 60s
                    print(f"Bedrock throttled, waiting {wait_time:.2f}s before retry {attempt + 1}/{max_retries}")
                    time.sleep(wait_time)
                else:
                    print(f"Bedrock failed after {max_retries} retries: {str(e)}")
                    raise e

        if any(f["name"] != "unknown" for f in faces):
            personalized = personalize_description(generic, faces)
            return personalized, generic

        return generic, generic

    except Exception as e:
        print(f"Caption error: {str(e)}")
        return "Image could not be fully described.", "Image could not be fully described."


# Helper function to convert float values to Decimal for DynamoDB
def convert_floats_to_decimal(obj):
    if isinstance(obj, float):
        return Decimal(str(obj))
    elif isinstance(obj, dict):
        return {k: convert_floats_to_decimal(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [convert_floats_to_decimal(i) for i in obj]
    else:
        return obj


def store_metadata(bucket, key, faces, labels, caption, generic_caption=None):
    try:
        table = dynamodb.Table(TABLE_NAME)
        names = [f["name"] for f in faces]
        
        # Get relationship metadata from Neptune
        relationship_metadata = get_relationship_metadata_neptune(names)
        
        # Store label metadata
        label_metadata = {
            'detected_labels': [{'name': label['name'], 'confidence': label['confidence']} for label in labels],
            'label_categories': {},
            'person_labels': {}
        }
        
        # Group labels by category
        for label in labels:
            for category in label.get('categories', []):
                if category not in label_metadata['label_categories']:
                    label_metadata['label_categories'][category] = []
                label_metadata['label_categories'][category].append(label['name'])
        
        # Associate labels with people
        for name in names:
            if name != 'unknown':
                label_metadata['person_labels'][name] = [label['name'] for label in labels]
        
        # Build searchable text with labels
        searchable_parts = [' '.join(names), caption]
        
        # Add labels to searchable text
        label_names = [label['name'] for label in labels]
        searchable_parts.extend(label_names)
        
        searchable = ' '.join(searchable_parts).lower()

        # Convert all float values to Decimal for DynamoDB
        faces_for_dynamo = convert_floats_to_decimal(faces)
        labels_for_dynamo = convert_floats_to_decimal(labels)
        relationship_metadata_dynamo = convert_floats_to_decimal(relationship_metadata)

        item = {
            "image_id": f"{bucket}/{key}",
            "bucket": bucket,
            "key": key,
            "faces": faces_for_dynamo,
            "labels": labels_for_dynamo,
            "caption": caption,
            "generic_caption": generic_caption,
            "relationship_metadata": relationship_metadata_dynamo,
            "label_metadata": convert_floats_to_decimal(label_metadata),
            "searchable_text": searchable,
            "timestamp": int(time.time()),
        }

        table.put_item(Item=item)
        print(f"Metadata stored for {bucket}/{key}")

    except Exception as e:
        print(f"DynamoDB storage error: {str(e)}")

def get_relationship_metadata_neptune(face_names):
    """Get relationship metadata for DynamoDB storage"""
    if not face_names:
        return {}
    
    try:
        from gremlin_python.driver.driver_remote_connection import DriverRemoteConnection
        from gremlin_python.process.anonymous_traversal import traversal
        from gremlin_python.process.graph_traversal import __
        
        neptune_endpoint = os.environ.get("NEPTUNE_ENDPOINT", "")
        if not neptune_endpoint:
            return {}
            
        connection = DriverRemoteConnection(f'wss://{neptune_endpoint}:8182/gremlin', 'g')
        g = traversal().withRemote(connection)
        
        metadata = {
            'people': [],
            'relationships': [],
            'family_roles': {}
        }
        
        # Get person metadata
        for name in face_names:
            try:
                person_vertices = g.V().has('name', name.lower()).valueMap().toList()
                if person_vertices:
                    person_data = person_vertices[0]
                    gender = person_data.get('gender', ['unknown'])[0] if person_data.get('gender') else 'unknown'
                    role = person_data.get('role', ['person'])[0] if person_data.get('role') else 'person'
                    
                    person_info = {
                        'name': name,
                        'gender': gender,
                        'role': role
                    }
                    metadata['people'].append(person_info)
                    metadata['family_roles'][name] = role
                else:
                    # Person doesn't exist in Neptune, create with default values
                    g.addV('person').property('name', name.lower()).property('gender', 'unknown').property('role', 'person').next()
                    metadata['people'].append({'name': name, 'gender': 'unknown', 'role': 'person'})
                    metadata['family_roles'][name] = 'person'
            except Exception as e:
                print(f"Error getting metadata for {name}: {str(e)}")
                metadata['people'].append({'name': name, 'gender': 'unknown', 'role': 'person'})
                metadata['family_roles'][name] = 'person'
        
        # Get relationships between people in this image
        for i, name1 in enumerate(face_names):
            for name2 in face_names[i+1:]:
                try:
                    edges = g.V().has('name', name1.lower()).bothE().where(
                        __.otherV().has('name', name2.lower())
                    ).valueMap(True).toList()
                    
                    for edge in edges:
                        relationship = {
                            'person1': name1,
                            'person2': name2,
                            'type': edge.get('type', ['unknown'])[0],
                            'description': edge.get('description', [''])[0]
                        }
                        metadata['relationships'].append(relationship)
                except:
                    pass
        
        connection.close()
        return metadata
        
    except Exception as e:
        print(f"Error getting relationship metadata: {str(e)}")
        return {}

def store_labels_in_neptune(labels, faces):
    """Store labels in Neptune and create relationships"""
    try:
        from gremlin_python.driver.driver_remote_connection import DriverRemoteConnection
        from gremlin_python.process.anonymous_traversal import traversal
        from gremlin_python.process.graph_traversal import __
        
        neptune_endpoint = os.environ.get("NEPTUNE_ENDPOINT", "")
        if not neptune_endpoint or not labels:
            return
            
        connection = DriverRemoteConnection(f'wss://{neptune_endpoint}:8182/gremlin', 'g')
        g = traversal().withRemote(connection)
        
        # Create label vertices and hierarchies
        for label in labels:
            label_name = label['name'].lower()
            confidence = label['confidence']
            categories = label.get('categories', [])
            
            # Add label vertex if it doesn't exist
            g.V().has('name', label_name).fold().coalesce(
                __.unfold(),
                __.addV('label')
                  .property('name', label_name)
                  .property('type', 'object')
                  .property('categories', ','.join(categories))
            ).next()
            
            # Create category hierarchy relationships
            for category in categories:
                category_name = category.lower()
                
                # Create category vertex if it doesn't exist
                g.V().has('name', category_name).fold().coalesce(
                    __.unfold(),
                    __.addV('category')
                      .property('name', category_name)
                      .property('type', 'category')
                ).next()
                
                # Create "belongs_to" relationship (label -> category)
                try:
                    g.V().has('name', label_name).addE('belongs_to').to(
                        __.V().has('name', category_name)
                    ).property('type', 'hierarchy').next()
                except:
                    pass
            
            # Create relationships between people and labels
            for face in faces:
                if face['name'] != 'unknown':
                    person_name = face['name'].lower()
                    
                    # Create "appears_with" relationship
                    try:
                        g.V().has('name', person_name).addE('appears_with').to(
                            __.V().has('name', label_name)
                        ).property('type', 'appears_with').property('confidence', confidence).next()
                    except:
                        pass
        
        # Create co-occurrence relationships
        create_label_cooccurrence(g, labels)
        
        connection.close()
        print(f"Stored {len(labels)} labels with hierarchies in Neptune")
        
    except Exception as e:
        print(f"Neptune label storage error: {str(e)}")

def create_label_cooccurrence(g, labels):
    """Create co-occurrence relationships between labels that appear together"""
    try:
        label_names = [label['name'].lower() for label in labels]
        
        # Create simple co-occurrence edges between all label pairs in this image
        for i, label1 in enumerate(label_names):
            for label2 in label_names[i+1:]:
                try:
                    # Create bidirectional co-occurrence edges
                    g.V().has('name', label1).addE('co_occurs_with').to(
                        g.V().has('name', label2)
                    ).property('type', 'co_occurrence').iterate()
                    
                    g.V().has('name', label2).addE('co_occurs_with').to(
                        g.V().has('name', label1)
                    ).property('type', 'co_occurrence').iterate()
                        
                except:
                    # Ignore errors - edges might already exist
                    pass
                    
    except Exception as e:
        print(f"Label co-occurrence error: {str(e)}")
