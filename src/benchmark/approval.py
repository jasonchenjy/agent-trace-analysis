"""Fail-closed approval gate. This module cannot grant approval."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess

from ..discovery_schema import validate_shape
from ..proposal_schema import PROPOSAL_SCHEMA
from ..proposal_contract import validate_contract
from ..review_gate import file_hash, load_object, require


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=True, allow_nan=False) + "\n", encoding="utf-8")


def apply_edits(proposal, edits):
    result = deepcopy(proposal)
    for edit in edits:
        parts = edit["path"].strip("/").split("/")
        node = result
        for part in parts[:-1]:
            node = node[int(part)] if isinstance(node, list) else node[part]
        key = int(parts[-1]) if isinstance(node, list) else parts[-1]
        require(node[key] == edit["before"], "approved edit precondition changed")
        node[key] = deepcopy(edit["after"])
    return result


def load_approved(path, manifest_path=None):
    path = Path(path).resolve()
    manifest_path = Path(manifest_path or path.with_name("approval_manifest.json")).resolve()
    manifest = load_object(manifest_path)
    root = (manifest_path.parent / manifest["repository_root"]).resolve()
    require(manifest["status"] == "approved" and manifest["approval"]["authority"] == "explicit_user_instruction",
            "human approval required")
    require(bool(manifest["approval"]["statement"]), "approval evidence missing")
    require(file_hash(path) == manifest["approved_specs_sha256"], "approved specification hash mismatch; review required")
    for entry in manifest["source_files"]:
        require(file_hash(root / entry["path"]) == entry["sha256"], f"source changed: {entry['path']}; review required")
    source = load_object(root / manifest["source"]["proposal_path"])
    review = load_object(root / manifest["review_path"])
    require(review["source"] == manifest["source"], "review source identity mismatch")
    raw = (root / manifest["source"]["proposal_path"]).read_bytes()
    blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
    require(blob == manifest["source"]["proposal_git_blob_sha"], "proposal Git blob mismatch")
    pinned = subprocess.run(["git", "rev-parse", f"{manifest['source']['commit']}:{manifest['source']['proposal_path']}"],
                            cwd=root, capture_output=True, text=True)
    require(pinned.returncode == 0, "reviewed source commit is unavailable; fetch the pinned history")
    pinned = pinned.stdout.strip()
    require(pinned == blob, "source commit does not contain reviewed proposal blob")
    require(source["dataset_sha256"] == manifest["source"]["dataset_sha256"], "dataset identity mismatch")
    specs = load_object(path)
    require(specs["status"] == "approved", "specs are not approved")
    originals = {p["proposal_id"]: p for p in source["proposals"]}
    decisions = {d["proposal_id"]: d for d in manifest["decisions"]}
    require(len(decisions) == len(manifest["decisions"]), "duplicate approval decisions")
    require(len(originals) == len(source["proposals"]), "duplicate source proposals")
    ids = [p["proposal_id"] for p in specs["proposals"]]
    require(len(ids) == len(set(ids)) and set(ids) == set(decisions) == set(originals), "approval set mismatch")
    for proposal in specs["proposals"]:
        decision = decisions[proposal["proposal_id"]]
        require(decision["status"] == "approved", "proposal lacks human approval")
        require(digest(proposal) == decision["approved_spec_sha256"], "individual specification changed")
        require(digest(originals[proposal["proposal_id"]]) == decision["source_spec_sha256"], "source proposal changed")
        require(apply_edits(originals[proposal["proposal_id"]], decision["edits"]) == proposal,
                "unreviewed specification edits")
        validate_shape(proposal, PROPOSAL_SCHEMA)
        validate_contract(proposal)
    return specs["proposals"], {"approved_specs_sha256": file_hash(path),
                                "approval_manifest_sha256": file_hash(manifest_path),
                                "source": manifest["source"], "source_files": manifest["source_files"]}
