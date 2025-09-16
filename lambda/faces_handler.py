import json
import os
from collections import defaultdict

import boto3

s3 = boto3.client("s3")


def is_image_file(key):
    return key.lower().endswith((".jpg", ".jpeg", ".png"))


def handler(event, context):
    bucket_name = os.environ["BUCKET_NAME"]
    faces = []

    try:
        # Step 1: List all images under the "faces/" prefix
        response = s3.list_objects_v2(Bucket=bucket_name, Prefix="faces/")
        contents = response.get("Contents", [])
        print("🧾 Found S3 keys:", [obj["Key"] for obj in contents])

        # Step 2: Group images by person_name (folder name)
        images_by_person = defaultdict(list)

        for obj in contents:
            key = obj["Key"]
            if is_image_file(key):
                parts = key.split("/")
                print(f"📁 Processing key: {key}, parts: {parts}")
                if len(parts) >= 3:
                    person_name = parts[1]
                    try:
                        presigned_url = s3.generate_presigned_url(
                            "get_object",
                            Params={"Bucket": bucket_name, "Key": key},
                            ExpiresIn=3600,
                        )
                    except Exception:
                        presigned_url = f"https://{bucket_name}.s3.us-west-2.amazonaws.com/{key}"

                    images_by_person[person_name].append(
                        {"file": parts[-1], "image_url": presigned_url}
                    )

        # Step 3: Assemble output format
        for person_name, image_list in images_by_person.items():
            faces.append({"person_name": person_name, "images": image_list})

        return {
            "statusCode": 200,
            "headers": {"Access-Control-Allow-Origin": "*"},
            "body": json.dumps({"faces": faces}),
        }

    except Exception as e:
        print(f"❌ Unhandled error: {str(e)}")
        return {
            "statusCode": 500,
            "headers": {"Access-Control-Allow-Origin": "*"},
            "body": json.dumps({"error": str(e)}),
        }
