import json
import os
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr

dynamodb = boto3.resource('dynamodb')
s3 = boto3.client('s3')

def convert_decimals(obj):
    """Convert Decimal objects to float for JSON serialization"""
    if isinstance(obj, list):
        return [convert_decimals(item) for item in obj]
    elif isinstance(obj, dict):
        return {key: convert_decimals(value) for key, value in obj.items()}
    elif isinstance(obj, Decimal):
        return float(obj)
    return obj

def extract_labels(labels_data):
    """Extract labels from DynamoDB format to simple format"""
    if not labels_data:
        return []
    
    try:
        # Handle both DynamoDB format and simple format
        if isinstance(labels_data, list) and len(labels_data) > 0:
            first_item = labels_data[0]
            # Check if it's DynamoDB format (has 'M' key)
            if isinstance(first_item, dict) and 'M' in first_item:
                # DynamoDB format: [{"M": {"name": {"S": "Photography"}}}]
                simple_labels = []
                for item in labels_data:
                    if 'M' in item:
                        label_data = item['M']
                        simple_label = {}
                        if 'name' in label_data and 'S' in label_data['name']:
                            simple_label['name'] = label_data['name']['S']
                        if 'confidence' in label_data and 'N' in label_data['confidence']:
                            simple_label['confidence'] = float(label_data['confidence']['N'])
                        if 'categories' in label_data and 'L' in label_data['categories']:
                            categories = [cat['S'] for cat in label_data['categories']['L'] if 'S' in cat]
                            simple_label['categories'] = categories
                        simple_labels.append(simple_label)
                return simple_labels
            else:
                # Already in simple format
                return labels_data
        return []
    except Exception as e:
        print(f"Error extracting labels: {str(e)}")
        return []

def handler(event, context):
    try:
        table = dynamodb.Table(os.environ['TABLE_NAME'])
        bucket_name = os.environ['BUCKET_NAME']
        
        query = event.get('queryStringParameters', {}) or {}
        search_term = query.get('q', '')
        
        # Check if this is a relationship query that Neptune should handle
        neptune_people = None
        if search_term:
            try:
                import sys
                sys.path.append('/opt/python')
                from neptune_search import parse_relationship_query
                neptune_people = parse_relationship_query(search_term)
                if neptune_people:
                    print(f"Neptune resolved '{search_term}' to people: {neptune_people}")
            except Exception as e:
                print(f"Neptune search error: {str(e)}")
                neptune_people = None
        
        # Get all items with pagination
        items_from_db = []
        scan_kwargs = {}
        
        if neptune_people:
            # Check if this is a "family" search (all members or person's family)
            if search_term and (search_term.lower() in ['family', 'family members', 'everyone', 'all family'] or 'family' in search_term.lower()):
                # For family search, use OR logic - image can contain any family member
                filter_expressions = []
                for person in neptune_people:
                    filter_expressions.append(Attr('searchable_text').contains(person.lower()))
                
                # Add filter to ensure images have recognized faces
                has_faces_filter = Attr('faces').exists() & Attr('faces').size().gt(0)
                
                if len(filter_expressions) == 1:
                    scan_kwargs['FilterExpression'] = filter_expressions[0] & has_faces_filter
                else:
                    combined_filter = filter_expressions[0]
                    for expr in filter_expressions[1:]:
                        combined_filter = combined_filter | expr  # OR logic for family
                    scan_kwargs['FilterExpression'] = combined_filter & has_faces_filter
                    
                print(f"Family search for any of: {neptune_people}")
            else:
                # Neptune resolved relationship query - search for images with these people
                filter_expressions = []
                for person in neptune_people:
                    filter_expressions.append(Attr('searchable_text').contains(person.lower()))
                
                # Add filter to ensure images have recognized faces
                has_faces_filter = Attr('faces').exists() & Attr('faces').size().gt(0)
                
                # Combine with AND logic - image must contain all people
                if len(filter_expressions) == 1:
                    scan_kwargs['FilterExpression'] = filter_expressions[0] & has_faces_filter
                else:
                    combined_filter = filter_expressions[0]
                    for expr in filter_expressions[1:]:
                        combined_filter = combined_filter & expr
                    scan_kwargs['FilterExpression'] = combined_filter & has_faces_filter
                    
                print(f"Neptune search for people: {neptune_people}")
            
        elif search_term:
            # Regular text search - split terms and use AND logic
            terms = search_term.lower().split()
            if len(terms) == 1:
                scan_kwargs['FilterExpression'] = Attr('searchable_text').contains(terms[0])
            else:
                # Multiple terms - all must be present (AND logic)
                filter_expressions = [Attr('searchable_text').contains(term) for term in terms]
                combined_filter = filter_expressions[0]
                for expr in filter_expressions[1:]:
                    combined_filter = combined_filter & expr
                scan_kwargs['FilterExpression'] = combined_filter
            print(f"Text search for: {search_term} (terms: {terms})")
        
        # Paginate through all results
        while True:
            response = table.scan(**scan_kwargs)
            items_from_db.extend(response['Items'])
            
            if 'LastEvaluatedKey' not in response:
                break
            scan_kwargs['ExclusiveStartKey'] = response['LastEvaluatedKey']
        
        items = []
        for item in items_from_db:
            # Extract image key from stored data
            if 'key' in item:
                s3_key = item['key']
                image_key = s3_key.split('/')[-1] if '/' in s3_key else s3_key
            else:
                # Extract from image_id if key not available
                image_id = item['image_id']
                if '/' in image_id:
                    s3_key = '/'.join(image_id.split('/')[1:])  # Remove bucket name
                    image_key = image_id.split('/')[-1]
                else:
                    s3_key = f"images/{image_id}"
                    image_key = image_id
            
            # Generate presigned URL with proper key handling
            try:
                presigned_url = s3.generate_presigned_url(
                    'get_object',
                    Params={'Bucket': bucket_name, 'Key': s3_key},
                    ExpiresIn=3600
                )
            except Exception as e:
                print(f"URL generation error for {s3_key}: {str(e)}")
                # Fallback URL with proper encoding
                from urllib.parse import quote
                encoded_key = quote(s3_key)
                presigned_url = f"https://{bucket_name}.s3.us-west-2.amazonaws.com/{encoded_key}"
            
            # Extract recognized faces and confidences from the faces list if present
            recognized_faces = []
            face_confidences = []
            if 'faces' in item and isinstance(item['faces'], list):
                for face_item in item['faces']:
                    if isinstance(face_item, dict) and 'name' in face_item:
                        # Only include faces that are not 'unknown'
                        if face_item['name'] != 'unknown':
                            recognized_faces.append(face_item['name'])
                            if 'confidence' in face_item:
                                face_confidences.append(face_item['confidence'])
            
            # For relationship searches, only include images with actual recognized faces
            if neptune_people and not recognized_faces:
                continue  # Skip images with no recognized faces for relationship searches
                
            items.append({
                'image_id': image_key,
                'caption': item.get('caption', 'No caption'),
                'recognized_faces': recognized_faces,
                'face_confidences': face_confidences,
                'labels': convert_decimals(extract_labels(item.get('labels', []))),
                'image_url': presigned_url
            })
        
        return {
            'statusCode': 200,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'results': items})
        }
    except Exception as e:
        return {
            'statusCode': 500,
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps({'error': str(e)})
        }