"""Technical preset metadata and a formatting-independent source fingerprint."""
import copy
import hashlib
import json
import xml.etree.ElementTree as ET

SVG = "http://www.w3.org/2000/svg"
BOXXER = "https://boxxer.local/ns/1"


def fingerprint(root):
    document = copy.deepcopy(root)

    def clean(node):
        for child in list(node):
            if child.tag.split("}")[-1] in ("metadata", "namedview"):
                node.remove(child)
            else:
                clean(child)
        if not (node.text or "").strip():
            node.text = None
        node.tail = None
        attributes = sorted((key, value) for key, value in node.attrib.items()
                            if not key.endswith("}docname"))
        node.attrib.clear()
        node.attrib.update(attributes)

    clean(document)
    return hashlib.sha256(ET.tostring(document, encoding="utf-8")).hexdigest()


def read_metadata(root):
    records = root.findall(f"./{{{SVG}}}metadata/{{{BOXXER}}}preset")
    if not records:
        return None
    if len(records) != 1:
        raise ValueError("SVG contains multiple preset metadata records")
    try:
        data = json.loads(records[0].text or "{}")
    except json.JSONDecodeError as error:
        raise ValueError("Invalid preset metadata") from error
    if not isinstance(data, dict) or data.get("schema") != 1:
        raise ValueError("Unsupported preset metadata schema")
    return {**data, "status": "ready" if data.get("geometry_sha256") == fingerprint(root) else "stale"}


def embed(root, data):
    for metadata in root.findall(f"{{{SVG}}}metadata"):
        for node in list(metadata):
            if node.tag == f"{{{BOXXER}}}preset":
                metadata.remove(node)
    metadata = root.find(f"{{{SVG}}}metadata")
    if metadata is None:
        metadata = ET.SubElement(root, f"{{{SVG}}}metadata")
    data = {**data, "schema": 1, "geometry_sha256": fingerprint(root)}
    ET.SubElement(metadata, f"{{{BOXXER}}}preset").text = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return data
