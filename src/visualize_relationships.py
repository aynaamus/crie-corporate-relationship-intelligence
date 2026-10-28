
import os
import textwrap
from pathlib import Path

import mysql.connector
import networkx as nx
import matplotlib.pyplot as plt
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", override=True)

TARGET_COMPANY = "CADBURY NIGERIA PLC"

# Retrieve documented relationships from MySQL
query = """
SELECT
    child.lei AS child_lei,
    child.legal_name AS child_name,
    parent.lei AS parent_lei,
    parent.legal_name AS parent_name,
    r.relationship_type

FROM corporate_relationships r

JOIN legal_entities child
    ON r.child_lei = child.lei

JOIN legal_entities parent
    ON r.parent_lei = parent.lei

WHERE child.legal_name = %s
  AND r.relationship_status = 'ACTIVE'
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
        cursor.execute(query, (TARGET_COMPANY,))
        records = cursor.fetchall()
    finally:
        cursor.close()
finally:
    connection.close()

if not records:
    raise ValueError(
        f"No relationships found for {TARGET_COMPANY}"
    )

# Construct the corporate network
graph = nx.DiGraph()
names = {}

for record in records:
    child = record["child_lei"]
    parent = record["parent_lei"]

    names[child] = record["child_name"]
    names[parent] = record["parent_name"]

    if not graph.has_edge(child, parent):
        graph.add_edge(child, parent, types=set())

    graph[child][parent]["types"].add(
        record["relationship_type"]
    )

# Prepare readable relationship labels
type_names = {
    "IS_DIRECTLY_CONSOLIDATED_BY": "Direct",
    "IS_ULTIMATELY_CONSOLIDATED_BY": "Ultimate",
}

edge_labels = {
    (u, v): " + ".join(
        type_names[t]
        for t in type_names
        if t in data["types"]
    )
    for u, v, data in graph.edges(data=True)
}

node_labels = {
    lei: textwrap.fill(name, width=20)
    for lei, name in names.items()
}

# Draw the graph
fig, ax = plt.subplots(figsize=(12, 8))
positions = nx.spring_layout(graph, seed=42)

nx.draw_networkx_nodes(
    graph,
    positions,
    node_size=5500,
    node_color="#dbeafe",
    edgecolors="#64748b",
    ax=ax
)

nx.draw_networkx_edges(
    graph,
    positions,
    arrows=True,
    arrowsize=22,
    node_size=5500,
    width=2,
    ax=ax
)

nx.draw_networkx_labels(
    graph,
    positions,
    labels=node_labels,
    font_size=9,
    font_weight="bold",
    ax=ax
)

nx.draw_networkx_edge_labels(
    graph,
    positions,
    edge_labels=edge_labels,
    font_size=9,
    rotate=False,
    ax=ax
)

ax.set_title(
    f"Corporate relationships: {TARGET_COMPANY}\n"
    "GLEIF sample — October 2026",
    fontsize=14,
    pad=20
)
ax.axis("off")

# Save the visualization
output_dir = ROOT / "reports"
output_dir.mkdir(exist_ok=True)

output = output_dir / "cadbury_relationships.png"

fig.savefig(
    output,
    dpi=180,
    bbox_inches="tight"
)
plt.close(fig)

print("Companies:", graph.number_of_nodes())
print("Unique entity pairs:", graph.number_of_edges())
print("Relationship records:", len(records))
print("Visualization saved:", output)
