import os
import re
from gremlin_python.driver.driver_remote_connection import DriverRemoteConnection
from gremlin_python.process.anonymous_traversal import traversal
from gremlin_python.process.graph_traversal import __

NEPTUNE_ENDPOINT = os.environ.get("NEPTUNE_ENDPOINT", "")

def parse_relationship_query(query):
    """Parse natural language queries into graph traversals"""
    query = query.lower().strip()
    
    # Expanded relationship terms
    parent_terms = r"(mother|mom|mama|mommy|mum|mummy|father|dad|daddy|papa|pop|parent|parents)"
    child_terms = r"(child|children|kid|kids|son|daughter|baby|babies)"
    sibling_terms = r"(sibling|siblings|brother|sister|bro|sis|big\s+brother|little\s+brother|big\s+sister|little\s+sister|older\s+brother|younger\s+brother|older\s+sister|younger\s+sister)"
    
    all_terms = f"{parent_terms}|{child_terms}|{sibling_terms}"
    
    # Pattern: Multi-step relationships (e.g., "alice's brother's mom")
    # Simplified pattern - look for word's word's word
    multi_step_simple = re.match(r"(\w+)'?s\s+(\w+)'?s\s+(\w+)", query)
    if multi_step_simple:
        person = multi_step_simple.group(1)
        first_relationship = multi_step_simple.group(2)
        second_relationship = multi_step_simple.group(3)
        print(f"Multi-step simple: '{query}' -> {person} -> {first_relationship} -> {second_relationship}")
        return find_multi_step_relationship(person, first_relationship, second_relationship)
    
    # Pattern: Multi-step relationships with complex terms
    multi_step_pattern = rf"(\w+)'?s?\s+({all_terms})'?s?\s+({all_terms})"
    multi_step_match = re.search(multi_step_pattern, query)
    if multi_step_match:
        person = multi_step_match.group(1)
        first_relationship = multi_step_match.group(2).strip()
        second_relationship = multi_step_match.group(3).strip()
        print(f"Multi-step complex: '{query}' -> {person} -> {first_relationship} -> {second_relationship}")
        return find_multi_step_relationship(person, first_relationship, second_relationship)
    
    # Pattern: "person's relationship" (e.g., "alice's mother", "bob's kids")
    possessive_match = re.match(rf"(\w+)'?s?\s+({all_terms})", query)
    if possessive_match:
        person = possessive_match.group(1)
        relationship = possessive_match.group(2)
        return find_related_people(person, relationship)
    
    # Pattern: "relationship of person" (e.g., "mother of alice", "kids of bob")
    of_match = re.match(rf"({all_terms})\s+of\s+(\w+)", query)
    if of_match:
        relationship = of_match.group(1)
        person = of_match.group(2)
        return find_related_people(person, relationship)
    
    # Pattern: "person relationship" (e.g., "alice mother", "bob daughter")
    # This should return both the person AND the related people
    simple_match = re.match(rf"(\w+)\s+({all_terms})", query)
    if simple_match:
        person = simple_match.group(1)
        relationship = simple_match.group(2)
        related_people = find_related_people(person, relationship)
        if related_people:
            # For non-possessive queries, include both the person and related people
            all_people = list(set([person] + related_people))
            print(f"Non-possessive query result: {all_people}")
            return all_people
        return None
    
    # Pattern: "family" - return all family members
    if query in ['family', 'family members', 'everyone', 'all family']:
        return find_all_family_members()
    
    # Pattern: "person's family" (e.g., "alice's family", "bob's family")
    family_match = re.match(r"(\w+)'?s?\s+family", query)
    if family_match:
        person = family_match.group(1)
        return find_person_family(person)
    
    return None

def find_related_people(person, relationship):
    """Use Neptune to find related people"""
    try:
        connection = DriverRemoteConnection(f'wss://{NEPTUNE_ENDPOINT}:8182/gremlin', 'g')
        g = traversal().withRemote(connection)
        
        related_people = []
        
        # Mother terms
        if relationship in ['mother', 'mom', 'mama', 'mommy', 'mum', 'mummy']:
            mothers = g.V().has('name', person).inE('parent_of').has('type', 'mother_of').outV().values('name').toList()
            related_people.extend(mothers)
            
        # Father terms
        elif relationship in ['father', 'dad', 'daddy', 'papa', 'pop']:
            fathers = g.V().has('name', person).inE('parent_of').has('type', 'father_of').outV().values('name').toList()
            related_people.extend(fathers)
            
        # Parent terms (both mother and father)
        elif relationship in ['parent', 'parents']:
            parents = g.V().has('name', person).inE('parent_of').outV().values('name').toList()
            related_people.extend(parents)
            
        # Child terms
        elif relationship in ['child', 'children', 'kid', 'kids', 'baby', 'babies']:
            children = g.V().has('name', person).outE('parent_of').inV().values('name').toList()
            related_people.extend(children)
            
        # Son terms
        elif relationship in ['son']:
            sons = g.V().has('name', person).outE('parent_of').inV().has('gender', 'boy').values('name').toList()
            related_people.extend(sons)
            
        # Daughter terms
        elif relationship in ['daughter']:
            daughters = g.V().has('name', person).outE('parent_of').inV().has('gender', 'girl').values('name').toList()
            related_people.extend(daughters)
            
        # General sibling terms
        elif relationship in ['sibling', 'siblings', 'bro', 'sis']:
            siblings = g.V().has('name', person).bothE('sibling_of').otherV().values('name').toList()
            related_people.extend(siblings)
            
        # Brother terms (including variations)
        elif relationship in ['brother', 'big brother', 'little brother', 'older brother', 'younger brother']:
            brothers = g.V().has('name', person).bothE('sibling_of').otherV().has('gender', 'boy').values('name').toList()
            related_people.extend(brothers)
            
        # Sister terms (including variations)
        elif relationship in ['sister', 'big sister', 'little sister', 'older sister', 'younger sister']:
            sisters = g.V().has('name', person).bothE('sibling_of').otherV().has('gender', 'girl').values('name').toList()
            related_people.extend(sisters)
        
        connection.close()
        
        # Return only the related people (not the original person)
        if related_people:
            # Remove duplicates, exclude original person
            result_people = list(set(related_people))
            print(f"find_related_people result: {result_people}")
            return result_people
        print(f"No related people found for {person} -> {relationship}")
        return None
        
    except Exception as e:
        print(f"Error finding related people: {str(e)}")
        return None

def find_multi_step_relationship(person, first_relationship, second_relationship):
    """Handle multi-step relationships like 'alice's sister's mom'"""
    try:
        print(f"Step 1: Finding {first_relationship} of {person}")
        # Step 1: Find first relationship
        first_step_people = find_related_people(person, first_relationship)
        print(f"Step 1 result: {first_step_people}")
        
        if not first_step_people:
            print("No first step people found")
            return None
        
        # Remove the original person, keep only the related people
        intermediate_people = [p for p in first_step_people if p != person]
        print(f"Intermediate people: {intermediate_people}")
        
        if not intermediate_people:
            print("No intermediate people found")
            return None
        
        # Step 2: Find second relationship for each intermediate person
        final_people = set()
        all_people_in_chain = set([person])  # Include original person
        
        for intermediate_person in intermediate_people:
            print(f"Step 2: Finding {second_relationship} of {intermediate_person}")
            second_step_people = find_related_people(intermediate_person, second_relationship)
            print(f"Step 2 result for {intermediate_person}: {second_step_people}")
            
            if second_step_people:
                # Add intermediate person to chain
                all_people_in_chain.add(intermediate_person)
                # Add final people (excluding intermediate person)
                for p in second_step_people:
                    if p != intermediate_person:
                        final_people.add(p)
                        all_people_in_chain.add(p)
        
        print(f"Final result: {list(all_people_in_chain)}")
        if final_people:
            return list(all_people_in_chain)
        return None
        
    except Exception as e:
        print(f"Error in multi-step relationship: {str(e)}")
        return None

def find_all_family_members():
    """Get all people in the Neptune database"""
    try:
        connection = DriverRemoteConnection(f'wss://{NEPTUNE_ENDPOINT}:8182/gremlin', 'g')
        g = traversal().withRemote(connection)
        
        # Get all person vertices
        all_people = g.V().hasLabel('person').values('name').toList()
        connection.close()
        
        print(f"All family members: {all_people}")
        return all_people if all_people else None
        
    except Exception as e:
        print(f"Error getting all family members: {str(e)}")
        return None

def find_person_family(person):
    """Get all family members related to a specific person"""
    try:
        connection = DriverRemoteConnection(f'wss://{NEPTUNE_ENDPOINT}:8182/gremlin', 'g')
        g = traversal().withRemote(connection)
        
        family_members = set([person])  # Include the person themselves
        
        # Get all people connected to this person (any relationship)
        connected_people = g.V().has('name', person).both().values('name').toList()
        family_members.update(connected_people)
        
        # Also get people connected through 2 degrees (e.g., siblings of parents)
        second_degree = g.V().has('name', person).both().both().values('name').toList()
        family_members.update(second_degree)
        
        connection.close()
        
        result = list(family_members)
        print(f"{person}'s family: {result}")
        return result if len(result) > 1 else None  # Return None if only the person themselves
        
    except Exception as e:
        print(f"Error getting {person}'s family: {str(e)}")
        return None

def search_images_with_people(people_list):
    """Search for images containing all people in the list"""
    if not people_list:
        return []
    
    # This will be called by the search handler to find images
    # containing the resolved people from Neptune
    return people_list