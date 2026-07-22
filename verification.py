"""Gemini Pro + Google Search vendor verification."""

from __future__ import annotations

import json
import logging
import os
import time

logger = logging.getLogger(__name__)

MODEL_ID = "gemini-2.5-pro"
MAX_RETRIES = 3

VERIFICATION_PROMPT = """### Role
You are a Technical Asset Discovery Specialist. Your goal is to verify if a vendor is a legitimate manufacturer of physical, network-connected OT or Medical hardware.

### Objective
Provide a technical "Physicality & Connectivity" report for the following vendor.

**Vendor:** {vendor_name}
**Website:** {vendor_url}

### Search & Validation Instructions (Hierarchical Evidence)
**CRITICAL INSTRUCTION:** You MUST use Google Search to verify. Do not rely on internal knowledge alone.

1. **Physical Trace:** Locate technical datasheets. Look for physical specs that software-only companies lack: dimensions, weight, power requirements (e.g., 24VDC, PoE), and operating temperature ranges.
2. **Connectivity Footprint:** Find evidence of a network stack. Search for "Default Credentials," "Initial IP Configuration," or "Network Port Mapping."
3. **Protocol Support:** Identify specific industrial or medical communication protocols supported (e.g., Modbus/TCP, Ethernet/IP, MQTT, DICOM, HL7, SNMP).
4. **Manufacturer Identity (OUI):** Check if the vendor (or their parent company/OEM) has a registered OUI (Organizationally Unique Identifier) in the IEEE MAC Address database.
5. **Support Infrastructure:** Does the vendor host a "Support Portal" or "Download Center" for firmware updates? (A key indicator of a real hardware lifecycle).

### Output Format
You MUST respond with valid JSON only, using this exact structure:
{{
    "verdict": "LEGIT or SOFTWARE-ONLY or SUSPICIOUS",
    "official_name": "The vendor's official company name with correct capitalization/branding",
    "website": "The vendor's official website URL",
    "hardware_evidence": "Describe 2-3 physical products found and their specific physical specs",
    "networking_proof": "Describe the method of network communication and any documented default network settings",
    "supported_protocols": ["List", "of", "discovered", "OT/IoMT", "protocols"],
    "mac_oui_check": "Yes/No/Unknown - Does the vendor have a registered MAC address block?",
    "technical_artifacts": ["Links to specific PDFs, Manuals, or Support Pages found"],
    "analyst_note": "Mention if this looks like a white-label reseller or an OEM",
    "device_types": ["List of device types this vendor manufactures"],
    "industries": ["Healthcare", "Industrial", "Enterprise", "etc."]
}}

IMPORTANT: Respond ONLY with valid JSON. No markdown, no explanation outside the JSON."""

FALLBACK_PROMPT = """### Role
Act as a Strict Hardware Inventory Auditor. Validate this vendor using Google Search.

**Vendor:** {vendor_name}
**Website:** {vendor_url}

Search for: official website, datasheets with physical specs, network connectivity evidence, IEEE OUI registry, firmware download pages.

**CONSTRAINT:** If search does not confirm physical networked hardware, classify as "SUSPICIOUS".

Return ONLY valid JSON:
{{
    "verdict": "LEGIT or SOFTWARE-ONLY or SUSPICIOUS",
    "official_name": "Official name with correct branding",
    "website": "Official URL",
    "hardware_evidence": "Physical products with specs",
    "networking_proof": "Network communication evidence",
    "supported_protocols": ["protocols"],
    "mac_oui_check": "Yes/No/Unknown",
    "technical_artifacts": ["URLs to datasheets/manuals"],
    "analyst_note": "OEM observations",
    "device_types": ["device types"],
    "industries": ["industries"]
}}"""

REQUIRED_FIELDS = {
    "verdict", "official_name", "website", "hardware_evidence",
    "networking_proof", "supported_protocols", "mac_oui_check",
    "technical_artifacts", "analyst_note", "device_types", "industries",
}
VALID_VERDICTS = {"LEGIT", "SOFTWARE-ONLY", "SUSPICIOUS"}


def _extract_json(text: str) -> dict | None:
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1].rsplit("\n", 1)[0]
    try:
        return json.loads(t)
    except Exception:
        start = t.find("{")
        end = t.rfind("}")
        if start != -1 and end != -1:
            try:
                return json.loads(t[start : end + 1])
            except Exception:
                pass
    return None


def _validate(data: dict) -> tuple[bool, list[str]]:
    errors = []
    missing = REQUIRED_FIELDS - set(data.keys())
    if missing:
        errors.append(f"Missing: {missing}")
    verdict = data.get("verdict")
    if verdict and verdict not in VALID_VERDICTS:
        errors.append(f"Bad verdict: {verdict}")
    return len(errors) == 0, errors


def verify_vendor(vendor_name: str, vendor_url: str | None, api_key: str) -> tuple[dict | None, str, bool]:
    """Returns (result_dict, raw_response, search_grounded)."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)

    for attempt in range(MAX_RETRIES):
        try:
            template = VERIFICATION_PROMPT if attempt < MAX_RETRIES - 1 else FALLBACK_PROMPT
            prompt = template.format(
                vendor_name=vendor_name,
                vendor_url=vendor_url or "Not provided",
            )
            config = types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
                temperature=0.0,
                max_output_tokens=4096,
            )
            response = client.models.generate_content(model=MODEL_ID, contents=[prompt], config=config)
            raw = response.text or ""
            data = _extract_json(raw)
            if data is None:
                raise ValueError("JSON parse failed")
            ok, errs = _validate(data)
            if not ok:
                raise ValueError(str(errs))
            return data, raw, True

        except Exception as e:
            logger.warning("Attempt %d/%d: %s", attempt + 1, MAX_RETRIES, e)
            if attempt < MAX_RETRIES - 1:
                time.sleep(2 * (attempt + 1))

    return None, "", False
