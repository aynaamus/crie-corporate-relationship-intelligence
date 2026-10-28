
import os
from pathlib import Path

import mysql.connector
import networkx as nx
import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", override=True)

# Retrieve active consolidation relationships
query = """
SELECT
    c.lei AS child_lei,
    c.legal_name AS child_name,
    p.lei AS parent_lei,
    p.legal_name AS parent_name,
    r.relationship_type
FROM corporate_relationships r
JOIN legal_entities c ON r.child_lei = c.lei
JOIN legal_entities p ON r.parent_lei = p.lei
WHERE r.relationship_status = 'ACTIVE'
AND r.registration_status = 'PUBLISHED'
AND r.relationship_type IN (
    'IS_DIRECTLY_CONSOLIDATED_BY',
    'IS_ULTIMATELY_CONSOLIDATED_BY'
)
"""

connection = mysql.connector.connect(
    host=os.getenv("MYSQL_HOST"),
    port=int(os.getenv("MYSQL_PORT", "3306")),
    user=os.getenv("MYSQL_USER"),
    password=os.getenv("MYSQL_PASSWORD"),
    database=os.getenv("MYSQL_DATABASE"),
)

try:
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(query)
        records = cursor.fetchall()
    finally:
        cursor.close()
finally:
    connection.close()

# Build a directed corporate relationship graph
G = nx.DiGraph()

for row in records:
    child = row["child_lei"]
    parent = row["parent_lei"]

    G.add_node(child, name=row["child_name"])
    G.add_node(parent, name=row["parent_name"])

    if not G.has_edge(child, parent):
        G.add_edge(child, parent, types=set())

    G[child][parent]["types"].add(
        row["relationship_type"]
    )

# Discover connected components
components = sorted(
    nx.weakly_connected_components(G),
    key=len,
    reverse=True
)

print("Total entities:", G.number_of_nodes())
print("Unique entity connections:", G.number_of_edges())
print("Relationship records:", len(records))
print("Connected groups:", len(components))

# Produce a membership report
results = []

for group_id, members in enumerate(components, 1):
    for lei in sorted(members):
        results.append({
            "group_id": group_id,
            "group_size": len(members),
            "lei": lei,
            "company": G.nodes[lei]["name"],
        })

df = pd.DataFrame(results)

output = ROOT / "reports"
output.mkdir(exist_ok=True)

df.to_csv(
    output / "corporate_groups.csv",
    index=False
)

print("\nLargest connected groups:")

for i, members in enumerate(components[:5], 1):
    print(f"\nGroup {i}: {len(members)} entities")

    for lei in sorted(members):
        print(" -", G.nodes[lei]["name"])

print("\nSaved: reports/corporate_groups.csv")
