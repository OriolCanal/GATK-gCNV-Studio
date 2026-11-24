from pathlib import Path

import yaml
from flask import current_app


def list_references():
    catalog = current_app.config['REFERENCE_CATALOG']
    if not catalog.is_file():
        return []
    try:
        data = yaml.safe_load(catalog.read_text(encoding='utf-8')) or {}
    except (OSError, yaml.YAMLError):
        return []

    available = []
    for reference_id, bundle in (data.get('references') or {}).items():
        resolved = dict(bundle)
        required_ok = True
        for key in ('fasta', 'fai', 'dict'):
            value = bundle.get(key)
            if not value or not Path(value).expanduser().is_file():
                required_ok = False
                break
            resolved[key] = str(Path(value).expanduser().resolve())
        if not required_ok:
            continue
        for key in ('mappability_track', 'segmental_duplication_track'):
            value = bundle.get(key)
            if value:
                if not Path(value).expanduser().is_file():
                    required_ok = False
                    break
                resolved[key] = str(Path(value).expanduser().resolve())
        if bundle.get('mappability_track') and not bundle.get('mappability_read_length'):
            required_ok = False
        if required_ok:
            available.append({
                'id': reference_id,
                'label': bundle.get('label', reference_id),
                'bundle_version': str(bundle.get('bundle_version') or ''),
                **resolved,
            })
    return available


def get_reference(reference_id):
    return next((ref for ref in list_references() if ref['id'] == reference_id), None)
