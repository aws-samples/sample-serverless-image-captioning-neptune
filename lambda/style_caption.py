import json
import os
from decimal import Decimal

import boto3

# AWS clients
bedrock = boto3.client("bedrock-runtime", region_name="us-west-2")
dynamodb = boto3.resource("dynamodb", region_name="us-west-2")

# Environment variables
TABLE_NAME = os.environ["TABLE_NAME"]
MODEL_ID = os.environ["MODEL_ID"]

def handler(event, context):
    try:
        print(f"Event: {json.dumps(event)}")

        # Parse request
        body = json.loads(event["body"])
        print(f"Request body: {body}")

        image_id = body["image_id"]
        style = body.get("style", "objective")
        print(f"Processing image_id: {image_id}, style: {style}")

        # Get image details from DynamoDB
        table = dynamodb.Table(TABLE_NAME)
        print(f"Looking up image in table: {TABLE_NAME}")
        response = table.get_item(Key={"image_id": image_id})
        print(f"DynamoDB response: {json.dumps(response, default=str)}")

        if "Item" not in response:
            print(f"Image not found: {image_id}")
            return {
                "statusCode": 404,
                "headers": {"Access-Control-Allow-Origin": "*"},
                "body": json.dumps({"error": "Image not found"})
            }

        item = response["Item"]
        caption = item.get("caption", "")
        faces = item.get("faces", [])

        # Get recognized face names
        face_names = [f["name"] for f in faces if f["name"] != "unknown"]
        
        # Generate styled caption from existing caption
        prompt = get_style_prompt(style, caption, face_names)

        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 250,
            "temperature": 0.5,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt}
                    ],
                }
            ],
        }

        res = bedrock.invoke_model(modelId=MODEL_ID, body=json.dumps(body))
        styled_caption = json.loads(res["body"].read())["content"][0]["text"]
        
        # Ensure names are mentioned
        if face_names:
            # Check if all names are in the caption
            missing_names = [name for name in face_names if name.lower() not in styled_caption.lower()]
            if missing_names:
                # Add missing names to the caption
                styled_caption += f" {', '.join(missing_names)} can be seen in the image."
                
        # Update DynamoDB with the new caption
        table.update_item(
            Key={"image_id": image_id},
            UpdateExpression="SET styled_caption = :caption, caption_style = :style",
            ExpressionAttributeValues={
                ":caption": styled_caption,
                ":style": style
            }
        )

        return {
            "statusCode": 200,
            "headers": {"Access-Control-Allow-Origin": "*"},
            "body": json.dumps({
                "caption": styled_caption,
                "style": style
            })
        }

    except Exception as e:
        print(f"Error: {str(e)}")
        return {
            "statusCode": 500,
            "headers": {"Access-Control-Allow-Origin": "*"},
            "body": json.dumps({"error": str(e)})
        }

def get_style_prompt(style, original_caption, face_names):
    """Generate a prompt based on the requested style"""
    names_str = ", ".join(face_names) if face_names else "people"
    
    # Add strong instruction to include names
    name_instruction = ""
    if face_names:
        name_instruction = f" You MUST mention these specific people by name: {names_str}."

    style_prompts = {
        "objective": f"Rewrite the following caption in a simple, factual style in 20-30 words. Focus on location, clothing, and activities.{name_instruction} Write in third person perspective.\n\nOriginal caption: \"{original_caption}\"",
        
        "warm": f"Rewrite the following caption in a warm, friendly style in 20-30 words. Focus on the positive emotions and connections between people.{name_instruction} Write in third person perspective.\n\nOriginal caption: \"{original_caption}\"",
        
        "funny": f"Rewrite the following caption in a humorous style in 20-30 words. Be light-hearted and playful, but not mean.{name_instruction} Write in third person perspective.\n\nOriginal caption: \"{original_caption}\"",
        
        "poetic": f"Rewrite the following caption in a poetic style in 20-30 words. Use vivid imagery and metaphors.{name_instruction} Write in third person perspective.\n\nOriginal caption: \"{original_caption}\"",
        
        "dramatic": f"Rewrite the following caption in a dramatic style in 20-30 words, as if it's a scene from a movie.{name_instruction} Write in third person perspective.\n\nOriginal caption: \"{original_caption}\""
    }
    
    return style_prompts.get(style.lower(), style_prompts["objective"])