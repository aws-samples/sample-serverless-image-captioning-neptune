import json
import os
import boto3
from gremlin_python.driver import client
from gremlin_python.driver.driver_remote_connection import DriverRemoteConnection
from gremlin_python.process.anonymous_traversal import traversal
from gremlin_python.process.graph_traversal import __
from gremlin_python.process.traversal import T

# Environment variables
NEPTUNE_ENDPOINT = os.environ.get("NEPTUNE_ENDPOINT", "")

def handler(event, context):
    """
    Neptune-based relationships handler for scaling to hundreds of people
    This is the future implementation when you need to scale beyond hardcoded relationships
    """
    try:
        if event.get("httpMethod") == "GET":
            return get_relationships()
        elif event.get("httpMethod") == "POST":
            body = json.loads(event["body"])
            if body.get("action") == "initialize":
                initialize_family_data()
                return {
                    "statusCode": 200,
                    "headers": {"Access-Control-Allow-Origin": "*"},
                    "body": json.dumps({"message": "Family data initialized"})
                }
            else:
                return add_relationship(body)
        else:
            return {
                "statusCode": 405,
                "headers": {"Access-Control-Allow-Origin": "*"},
                "body": json.dumps({"error": "Method not allowed"})
            }
    except Exception as e:
        print(f"Error: {str(e)}")
        return {
            "statusCode": 500,
            "headers": {"Access-Control-Allow-Origin": "*"},
            "body": json.dumps({"error": str(e)})
        }

def get_relationships():
    """Get all relationships from Neptune"""
    try:
        connection = DriverRemoteConnection(f'wss://{NEPTUNE_ENDPOINT}:8182/gremlin', 'g')
        g = traversal().withRemote(connection)
        
        relationships = []
        edges = g.E().valueMap(True).toList()
        
        for edge in edges:
            out_vertex = g.E(edge[T.id]).outV().values('name').next()
            in_vertex = g.E(edge[T.id]).inV().values('name').next()
            relationship_type = edge.get("type", [""])[0]
            
            relationships.append({
                "from": out_vertex,
                "to": in_vertex,
                "relationship": relationship_type
            })
        
        connection.close()
        
        return {
            "statusCode": 200,
            "headers": {"Access-Control-Allow-Origin": "*"},
            "body": json.dumps({"relationships": relationships})
        }
    except Exception as e:
        print(f"Error getting relationships: {str(e)}")
        return {
            "statusCode": 500,
            "headers": {"Access-Control-Allow-Origin": "*"},
            "body": json.dumps({"error": str(e)})
        }

def add_relationship(data):
    """Add a relationship with person details to Neptune"""
    try:
        person1 = data["person1"]
        person2 = data["person2"]
        relationship_type = data["relationship"]
        description = data.get("description", "")
        
        # Optional person details
        person1_details = data.get("person1_details", {})
        person2_details = data.get("person2_details", {})
        
        connection = DriverRemoteConnection(f'wss://{NEPTUNE_ENDPOINT}:8182/gremlin', 'g')
        g = traversal().withRemote(connection)
        
        # Add person1 with details if provided
        vertex1 = g.V().has('name', person1).fold().coalesce(
            __.unfold(),
            __.addV('person').property('name', person1)
        )
        
        if person1_details:
            for key, value in person1_details.items():
                vertex1 = vertex1.property(key, value)
        vertex1.next()
        
        # Add person2 with details if provided
        vertex2 = g.V().has('name', person2).fold().coalesce(
            __.unfold(),
            __.addV('person').property('name', person2)
        )
        
        if person2_details:
            for key, value in person2_details.items():
                vertex2 = vertex2.property(key, value)
        vertex2.next()
        
        # Add edge with description
        edge = g.V().has('name', person1).addE(relationship_type).to(
            __.V().has('name', person2)
        ).property('type', relationship_type)
        
        if description:
            edge = edge.property('description', description)
        edge.next()
        
        connection.close()
        
        return {
            "statusCode": 200,
            "headers": {"Access-Control-Allow-Origin": "*"},
            "body": json.dumps({"message": "Relationship added successfully"})
        }
    except Exception as e:
        print(f"Error adding relationship: {str(e)}")
        return {
            "statusCode": 500,
            "headers": {"Access-Control-Allow-Origin": "*"},
            "body": json.dumps({"error": str(e)})
        }

def get_person_relationships(person_name):
    """Get all relationships for a specific person - useful for caption context"""
    try:
        connection = DriverRemoteConnection(f'wss://{NEPTUNE_ENDPOINT}:8182/gremlin', 'g')
        g = traversal().withRemote(connection)
        
        relationships = []
        
        # Get outgoing relationships
        outgoing = g.V().has('name', person_name).outE().valueMap(True).toList()
        for edge in outgoing:
            target = g.E(edge[T.id]).inV().values('name').next()
            relationships.append({
                "from": person_name,
                "to": target,
                "relationship": edge.get("type", [""])[0]
            })
        
        # Get incoming relationships
        incoming = g.V().has('name', person_name).inE().valueMap(True).toList()
        for edge in incoming:
            source = g.E(edge[T.id]).outV().values('name').next()
            relationships.append({
                "from": source,
                "to": person_name,
                "relationship": edge.get("type", [""])[0]
            })
        
        connection.close()
        return relationships
        
    except Exception as e:
        print(f"Error getting person relationships: {str(e)}")
        return []

def get_relationship_context_neptune(face_names):
    """
    Get natural relationship context for captions (indirect, no ages)
    """
    if not face_names or len(face_names) < 2:
        return ""
    
    try:
        connection = DriverRemoteConnection(f'wss://{NEPTUNE_ENDPOINT}:8182/gremlin', 'g')
        g = traversal().withRemote(connection)
        
        # Get relationship info for pairs
        for i, name1 in enumerate(face_names):
            for name2 in face_names[i+1:]:
                # Check if there's a direct relationship between name1 and name2
                edges = g.V().has('name', name1.lower()).bothE().where(
                    __.otherV().has('name', name2.lower())
                ).valueMap(True).toList()
                
                for edge in edges:
                    rel_type = edge.get("type", [""])[0]
                    if rel_type == "mother_of":
                        return " Context: This shows a mother and child together."
                    elif rel_type == "father_of":
                        return " Context: This shows a father and child together."
                    elif rel_type == "sibling_of":
                        return " Context: This shows siblings together."
                    elif rel_type == "spouse_of":
                        return " Context: This shows a married couple."
        
        connection.close()
        return ""
        
    except Exception as e:
        print(f"Error getting Neptune relationship context: {str(e)}")
        return ""

def get_relationship_metadata(face_names):
    """
    Get detailed relationship metadata for search functionality
    """
    if not face_names:
        return {}
    
    try:
        connection = DriverRemoteConnection(f'wss://{NEPTUNE_ENDPOINT}:8182/gremlin', 'g')
        g = traversal().withRemote(connection)
        
        metadata = {}
        
        for name in face_names:
            try:
                person_vertices = g.V().has('name', name.lower()).valueMap().toList()
                if person_vertices:
                    person_data = person_vertices[0]
                    gender = person_data.get('gender', ['unknown'])[0] if person_data.get('gender') else 'unknown'
                    role = person_data.get('role', ['person'])[0] if person_data.get('role') else 'person'
                    
                    metadata[name.lower()] = {
                        "gender": gender,
                        "role": role
                    }
                else:
                    metadata[name.lower()] = {
                        "gender": "unknown",
                        "role": "person"
                    }
            except Exception as e:
                print(f"Error getting metadata for {name}: {str(e)}")
                metadata[name.lower()] = {
                    "gender": "unknown",
                    "role": "person"
                }
                
                # Get relationships for this person
                relationships = []
                
                # Outgoing relationships
                outgoing = g.V().has('name', name.lower()).outE().valueMap(True).toList()
                for edge in outgoing:
                    target = g.E(edge[T.id]).inV().values('name').next()
                    rel_type = edge.get("type", [""])[0] if edge.get("type") else ""
                    # Handle truncated relationship types
                    if rel_type == "m":
                        rel_type = "mother_of"
                    elif rel_type == "s":
                        rel_type = "sibling_of"
                    relationships.append({"to": target, "type": rel_type})
                
                # Incoming relationships
                incoming = g.V().has('name', name.lower()).inE().valueMap(True).toList()
                for edge in incoming:
                    source = g.E(edge[T.id]).outV().values('name').next()
                    rel_type = edge.get("type", [""])[0] if edge.get("type") else ""
                    # Handle truncated relationship types
                    if rel_type == "m":
                        rel_type = "mother_of"
                    elif rel_type == "s":
                        rel_type = "sibling_of"
                    relationships.append({"from": source, "type": rel_type})
                
                metadata[name.lower()]["relationships"] = relationships
        
        connection.close()
        return metadata
        
    except Exception as e:
        print(f"Error getting relationship metadata: {str(e)}")
        return {}

def initialize_family_data():
    """Initialize the family with age, gender, and relationship data in Neptune
    
    Note: This is a sample made-up family relationship for demo purposes only.
    The names Alice, Bob, and Charlie are fictional and used for demonstration.
    """
    try:
        connection = DriverRemoteConnection(f'wss://{NEPTUNE_ENDPOINT}:8182/gremlin', 'g')
        g = traversal().withRemote(connection)
        
        # Add people with gender and role properties
        # Note: This is sample demo data with fictional family members
        people_data = [
            {"name": "alice", "gender": "woman", "role": "mother"},
            {"name": "bob", "gender": "girl", "role": "daughter"},
            {"name": "charlie", "gender": "boy", "role": "son"}
        ]
        
        print("Starting Neptune initialization...")
        
        # Clear existing data first
        try:
            g.V().drop().iterate()
            print("Cleared existing data")
        except:
            print("No existing data to clear")
        
        # Add people with correct properties
        for person in people_data:
            try:
                vertex = g.addV('person')\
                  .property('name', person["name"])\
                  .property('gender', person["gender"])\
                  .property('role', person["role"]).next()
                print(f"Successfully added {person['name']}: gender={person['gender']}, role={person['role']}")
                
                # Verify it was added
                check = g.V().has('name', person["name"]).valueMap().next()
                print(f"Verification for {person['name']}: {check}")
            except Exception as e:
                print(f"Error adding {person['name']}: {str(e)}")
        
        # Add relationships with detailed properties
        # Alice is the mother of Bob and Charlie
        g.V().has('name', 'alice').addE('parent_of').to(
            __.V().has('name', 'bob')
        ).property('type', 'mother_of').property('description', 'Alice is Bob\'s mother').next()
        
        g.V().has('name', 'alice').addE('parent_of').to(
            __.V().has('name', 'charlie')
        ).property('type', 'mother_of').property('description', 'Alice is Charlie\'s mother').next()
        
        # Bob and Charlie are siblings
        g.V().has('name', 'bob').addE('sibling_of').to(
            __.V().has('name', 'charlie')
        ).property('type', 'sibling_of').property('description', 'Bob and Charlie are siblings').next()
        
        g.V().has('name', 'charlie').addE('sibling_of').to(
            __.V().has('name', 'bob')
        ).property('type', 'sibling_of').property('description', 'Bob and Charlie are siblings').next()
        
        # Debug: Check what was actually stored
        for person in people_data:
            stored_data = g.V().has('name', person["name"]).valueMap().next()
            print(f"Stored {person['name']}: {stored_data}")
        
        connection.close()
        print("Family data with gender and relationships initialized successfully in Neptune")
        
    except Exception as e:
        print(f"Error initializing family data: {str(e)}")

