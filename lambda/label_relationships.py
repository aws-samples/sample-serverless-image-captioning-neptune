import json
import os
from gremlin_python.driver.driver_remote_connection import DriverRemoteConnection
from gremlin_python.process.anonymous_traversal import traversal

def handler(event, context):
    try:
        neptune_endpoint = os.environ.get("NEPTUNE_ENDPOINT", "")
        if not neptune_endpoint:
            return {
                "statusCode": 500,
                "headers": {"Access-Control-Allow-Origin": "*"},
                "body": json.dumps({"error": "Neptune endpoint not configured"})
            }
        
        query_params = event.get('queryStringParameters', {}) or {}
        query_type = query_params.get('type', '')
        name = query_params.get('name', '')
        
        if not name:
            return {
                "statusCode": 400,
                "headers": {"Access-Control-Allow-Origin": "*"},
                "body": json.dumps({"error": "Name parameter required"})
            }
        
        connection = DriverRemoteConnection(f'wss://{neptune_endpoint}:8182/gremlin', 'g')
        g = traversal().withRemote(connection)
        
        results = []
        
        if query_type == 'person_labels':
            # Find labels that a person appears with (deduplicated, filtered for relevance)
            all_labels = g.V().has('name', name.lower()).outE('appears_with').inV().dedup().valueMap().toList()
            
            # Filter out generic labels and prioritize meaningful ones
            generic_labels = {'person', 'people', 'face', 'head', 'adult', 'male', 'female', 'man', 'woman'}
            filtered_labels = []
            
            for label in all_labels:
                label_name = label.get('name', [''])[0]
                if label_name not in generic_labels:
                    filtered_labels.append({
                        'name': label_name,
                        'type': label.get('type', [''])[0],
                        'categories': label.get('categories', [''])[0].split(',') if label.get('categories', ['']) else []
                    })
            
            # Return top 5 meaningful labels
            results = filtered_labels[:5]
                
        elif query_type == 'label_people':
            # Find people who appear with a specific label (deduplicated, top 5)
            people = g.V().has('name', name.lower()).inE('appears_with').outV().dedup().limit(5).valueMap().toList()
            
            for person in people:
                results.append({
                    'name': person.get('name', [''])[0],
                    'age': person.get('age', [''])[0],
                    'gender': person.get('gender', [''])[0],
                    'role': person.get('role', [''])[0]
                })
                
        elif query_type == 'common_labels':
            # Find labels that two people commonly appear with
            person2 = query_params.get('person2', '')
            if not person2:
                return {
                    "statusCode": 400,
                    "headers": {"Access-Control-Allow-Origin": "*"},
                    "body": json.dumps({"error": "person2 parameter required for common_labels"})
                }
            
            # Get labels for person 1
            labels1 = set(g.V().has('name', name.lower()).outE('appears_with').inV().values('name').toList())
            # Get labels for person 2  
            labels2 = set(g.V().has('name', person2.lower()).outE('appears_with').inV().values('name').toList())
            # Find intersection
            common = labels1.intersection(labels2)
            results = list(common)
            
        elif query_type == 'label_hierarchy':
            # Find category hierarchy for a label
            categories = g.V().has('name', name.lower()).outE('belongs_to').inV().valueMap().toList()
            for category in categories:
                results.append({
                    'name': category.get('name', [''])[0],
                    'type': 'category'
                })
                
        elif query_type == 'label_cooccurrence':
            # Find labels that commonly appear with this label (top 5)
            cooccurring = g.V().has('name', name.lower()).outE('co_occurs_with').limit(5).inV().valueMap().toList()
            for vertex in cooccurring:
                results.append({
                    'name': vertex.get('name', [''])[0],
                    'type': 'co_occurrence'
                })
                
        elif query_type == 'category_labels':
            # Find all labels in a category
            category_labels = g.V().has('name', name.lower()).inE('belongs_to').outV().valueMap().toList()
            for label in category_labels:
                results.append({
                    'name': label.get('name', [''])[0],
                    'categories': label.get('categories', [''])[0].split(',') if label.get('categories', ['']) else []
                })
        
        connection.close()
        
        return {
            "statusCode": 200,
            "headers": {"Access-Control-Allow-Origin": "*"},
            "body": json.dumps({"results": results})
        }
        
    except Exception as e:
        return {
            "statusCode": 500,
            "headers": {"Access-Control-Allow-Origin": "*"},
            "body": json.dumps({"error": str(e)})
        }