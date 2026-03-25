"""OpenSRS XML envelope building and response parsing utilities."""
from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape, quoteattr


class OpenSRSError(Exception):
    """Error returned by the OpenSRS API."""

    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(f"OpenSRS error {code}: {message}")


def sign(xml_body: str, api_key: str) -> str:
    """Compute MD5 signature: md5(md5(xml + key) + key)."""
    inner = hashlib.md5((xml_body + api_key).encode()).hexdigest()
    return hashlib.md5((inner + api_key).encode()).hexdigest()


def dict_to_xml(d: dict | list) -> str:
    """Convert a dict or list to OpenSRS XML format (dt_assoc/dt_array)."""
    if isinstance(d, list):
        items = []
        for i, v in enumerate(d):
            if isinstance(v, (dict, list)):
                items.append(f'<item key="{i}">{dict_to_xml(v)}</item>')
            elif v is None:
                items.append(f'<item key="{i}"></item>')
            else:
                items.append(f'<item key="{i}">{escape(str(v))}</item>')
        return f"<dt_array>{' '.join(items)}</dt_array>"

    items = []
    for key, value in d.items():
        attr_key = quoteattr(str(key))
        if isinstance(value, (dict, list)):
            items.append(f'<item key={attr_key}>{dict_to_xml(value)}</item>')
        elif value is None:
            items.append(f'<item key={attr_key}></item>')
        else:
            items.append(f'<item key={attr_key}>{escape(str(value))}</item>')
    return f"<dt_assoc>{' '.join(items)}</dt_assoc>"


def build_envelope(action: str, obj: str, attrs: dict) -> str:
    """Build an OpenSRS XML request envelope."""
    attrs_xml = dict_to_xml(attrs)
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="no"?>'
        '<!DOCTYPE OPS_envelope SYSTEM "ops.dtd">'
        "<OPS_envelope>"
        "<header><version>0.9</version></header>"
        "<body>"
        "<data_block>"
        "<dt_assoc>"
        f'<item key="protocol">XCP</item>'
        f'<item key="action">{action}</item>'
        f'<item key="object">{obj}</item>'
        f'<item key="attributes">{attrs_xml}</item>'
        "</dt_assoc>"
        "</data_block>"
        "</body>"
        "</OPS_envelope>"
    )


def _parse_assoc(elem: ET.Element) -> dict:
    """Parse a dt_assoc element into a dict."""
    result = {}
    for item in elem.findall("item"):
        key = item.get("key")
        child_assoc = item.find("dt_assoc")
        child_array = item.find("dt_array")
        if child_assoc is not None:
            result[key] = _parse_assoc(child_assoc)
        elif child_array is not None:
            result[key] = _parse_array(child_array)
        else:
            result[key] = item.text or ""
    return result


def _parse_array(elem: ET.Element) -> list:
    """Parse a dt_array element into a list."""
    items = []
    for item in elem.findall("item"):
        child_assoc = item.find("dt_assoc")
        child_array = item.find("dt_array")
        if child_assoc is not None:
            items.append(_parse_assoc(child_assoc))
        elif child_array is not None:
            items.append(_parse_array(child_array))
        else:
            items.append(item.text or "")
    return items


def parse_response(xml_text: str) -> dict:
    """Parse an OpenSRS XML response into a dict.

    Raises OpenSRSError if the response indicates failure.
    """
    root = ET.fromstring(xml_text)
    body_assoc = root.find(".//body/data_block/dt_assoc")
    if body_assoc is None:
        raise OpenSRSError(0, "Malformed response: no data_block dt_assoc found")

    data = _parse_assoc(body_assoc)

    if data.get("is_success") != "1":
        code = int(data.get("response_code", 0))
        message = data.get("response_text", "Unknown error")
        raise OpenSRSError(code, message)

    return data
