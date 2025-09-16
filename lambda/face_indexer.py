import json
import os
from urllib.parse import unquote_plus

import boto3

rekognition = boto3.client("rekognition")
s3 = boto3.client("s3")


def handler(event, context):
    try:
        if "Records" in event:
            bucket_name = event["Records"][0]["s3"]["bucket"]["name"]
            key = unquote_plus(event["Records"][0]["s3"]["object"]["key"])
        else:
            body = json.loads(event["body"])
            key = body["image_key"]
            bucket_name = os.environ["BUCKET_NAME"]

        collection_id = os.environ["COLLECTION_ID"]

        # Create collection if needed
        try:
            rekognition.create_collection(CollectionId=collection_id)
            print(f"ℹ️ Collection {collection_id} created.")
        except rekognition.exceptions.ResourceAlreadyExistsException:
            print(f"ℹ️ Collection {collection_id} already exists")

        indexed_faces = []

        if key.endswith("/"):
            # Folder path: index all valid image files inside
            response = s3.list_objects_v2(Bucket=bucket_name, Prefix=key)
            image_keys = [
                obj["Key"]
                for obj in response.get("Contents", [])
                if not obj["Key"].endswith("/") and is_image_file(obj["Key"])
            ]
            if not image_keys:
                raise Exception("No images found under folder.")

            person_name = extract_person_name_from_path(key)
            print(
                f"📂 Indexing folder '{person_name}' with {len(image_keys)} image(s)"
            )

            for img_key in image_keys:
                if index_face(bucket_name, img_key, person_name, collection_id):
                    indexed_faces.append(img_key)
        else:
            person_name = extract_person_name_from_path(key)
            if not person_name:
                raise Exception(f"Invalid path: {key}")
            if index_face(bucket_name, key, person_name, collection_id):
                indexed_faces.append(key)

        print("✅ Indexed faces:")
        for face in indexed_faces:
            print(f"- {person_name}: {face}")

        return {
            "statusCode": 200,
            "headers": {"Access-Control-Allow-Origin": "*"},
            "body": json.dumps({"indexed_faces": indexed_faces}),
        }

    except Exception as e:
        import traceback

        print(traceback.format_exc())
        return {
            "statusCode": 500,
            "headers": {"Access-Control-Allow-Origin": "*"},
            "body": json.dumps({"error": str(e)}),
        }


def extract_person_name_from_path(key):
    """Extract person name from path like 'faces/alice/alice1.jpg' -> 'alice'"""
    parts = key.split("/")
    if len(parts) >= 3 and parts[0] == "faces":
        return parts[1]
    return None


def is_image_file(key):
    return key.lower().endswith((".jpg", ".jpeg", ".png"))


def index_face(bucket, image_key, person_name, collection_id):
    """Index a single face image using Rekognition"""
    print(f"🧠 Indexing image {image_key} as ExternalImageId={person_name}")

    try:
        rekognition.index_faces(
            CollectionId=collection_id,
            Image={"S3Object": {"Bucket": bucket, "Name": image_key}},
            ExternalImageId=person_name,  # ✅ use consistent person name
            DetectionAttributes=["DEFAULT"],
            QualityFilter="AUTO",
        )
        return True
    except Exception as e:
        print(f"❌ Failed indexing {image_key}: {e}")
        return False
