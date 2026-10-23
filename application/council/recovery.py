"""Read-only archive reconstruction and explicit revision recovery recipes.

No inference is dispatched here. Original archives and context files are never
written. Ordinary bodies live once in the JSONL stream (legacy: byte-counted
transcript spans), while coordinator acceptance/routing remains machine data.
"""

from collections import OrderedDict
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import re
import stat

from . import run_archive, context_sources

SOURCE_SHA256 = sha256(Path(__file__).read_bytes()).hexdigest()
FROZEN_CONTEXTS = OrderedDict()
RUN_ID = re.compile(r"\d{8}-\d{4}(?:-\d{2,})?\Z")
LEGACY_TEMPLATE_SHA256 = "00c9a1fc0b16bcc427da009776d9e96bbfdf09c04ae67bf7ca20b29ced002da5"


class RecoveryUnavailable(ValueError): pass


def remember_context(run_id, bundle):
    # Small in-process cache only; no on-disk source snapshots or retained SDKs.
    FROZEN_CONTEXTS[run_id] = (bundle.text, bundle.metadata_json)
    FROZEN_CONTEXTS.move_to_end(run_id)
    while len(FROZEN_CONTEXTS) > 3: FROZEN_CONTEXTS.popitem(last=False)


def safe_path(run_id, suffix):
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        raise RecoveryUnavailable("Invalid run identity")
    root = run_archive.LOGS_DIR
    folder = root / (run_id + "-Logs")
    path = folder / (run_id + suffix)
    for candidate in (root, folder, path):
        try: info = candidate.lstat()
        except OSError: raise RecoveryUnavailable("Required run record is unavailable") from None
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise RecoveryUnavailable("Linked run records are not supported")
    return path


@dataclass
class RunData:
    run_id: str
    records: list
    interrupted_tail: bool = False
    active: bool = False


def read_run(run_id):
    live = run_archive.LIVE_ARCHIVES.get(run_id)
    if live is not None and live.paths and live.paths[0].parent.parent == run_archive.LOGS_DIR:
        return RunData(run_id, json.loads(json.dumps(live.records)), active=live.active)
    raw = safe_path(run_id, "-Diagnostics.jsonl").read_bytes()
    rows, tail = [], False
    for line in raw.splitlines(keepends=True):
        if not line.endswith(b"\n"):
            tail = True
            break  # An unflushed terminal record is not a terminal outcome.
        try: row = json.loads(line)
        except (ValueError, UnicodeError): raise RecoveryUnavailable("Malformed interior diagnostic record") from None
        if row.get("run_id") != run_id: raise RecoveryUnavailable("Conflicting run identity")
        rows.append(row)
    by_sequence = {}
    for row in rows:
        seq = row.get("sequence")
        if type(seq) is not int: raise RecoveryUnavailable("Missing event sequence")
        if seq in by_sequence and by_sequence[seq] != row:
            raise RecoveryUnavailable("Conflicting event sequence")
        by_sequence[seq] = row
    return RunData(run_id, [by_sequence[s] for s in sorted(by_sequence)], tail)


def run_sort(run_id):
    return run_id[:13], int(run_id[14:]) if len(run_id) > 13 else 1


def run_ids():
    root = run_archive.LOGS_DIR
    try: info = root.lstat()
    except FileNotFoundError: return []
    if stat.S_ISLNK(info.st_mode) or getattr(info,"st_file_attributes",0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise RecoveryUnavailable("Linked archive root is not supported")
    return sorted({p.name[:-5] for p in root.iterdir() if p.name.endswith("-Logs") and RUN_ID.fullmatch(p.name[:-5])}, key=run_sort)


def _legacy_bodies(data):
    """Replay recorder formatting and exact byte lengths; never parse model headings.

    Single ordinary blocks are reconstructible in the old format. Multiple blocks
    have no recorded boundaries, so that case is explicitly unavailable.
    """
    raw = safe_path(data.run_id, "-Transcript.md").read_bytes()
    renderer = object.__new__(run_archive.RunArchive)
    renderer.run_id = data.run_id
    cursor, bodies = 0, {}
    for record in data.records:
        markdown = renderer._markdown(record, [])
        if record["event"] == "provider_completed":
            count, length = record.get("text_block_count"), record.get("response_text_bytes")
            if count not in (0, 1) or type(length) is not int:
                raise RecoveryUnavailable("Legacy ordinary block boundaries are insufficient for exact reconstruction")
            prefix = markdown[:-2].encode("utf-8")
            if not raw.startswith(prefix, cursor): raise RecoveryUnavailable("Legacy transcript/diagnostic framing disagrees")
            cursor += len(prefix)
            body = raw[cursor:cursor + length]
            if len(body) != length: raise RecoveryUnavailable("Legacy response text is incomplete")
            cursor += length
            if raw[cursor:cursor + 2] != b"\n\n": raise RecoveryUnavailable("Legacy response byte boundary disagrees")
            cursor += 2
            bodies[record["stage"], record["provider"]] = body.decode("utf-8")
        else:
            fragment = markdown.encode("utf-8")
            if not raw.startswith(fragment, cursor): raise RecoveryUnavailable("Legacy recorder framing is incomplete")
            cursor += len(fragment)
    if cursor != len(raw): raise RecoveryUnavailable("Unaccounted legacy transcript content")
    return bodies


def response_texts(data):
    if not data.records: return {}
    start = next((r for r in data.records if r["event"] == "run_started"), {})
    if start.get("schema_version", 1) < 2: return _legacy_bodies(data)
    blocks, results = {}, {}
    for row in data.records:
        key = row.get("stage"), row.get("provider")
        if row["event"] == "provider_text":
            per_block = blocks.setdefault(key, {})
            index = row["block_index"]
            per_block[index] = per_block.get(index, "") + row["text_delta"]
        elif row["event"] == "provider_completed" and "ordinary_blocks" in row:
            results[key] = " ".join(row["ordinary_blocks"])
    results.update({key: " ".join(parts[i] for i in sorted(parts)) for key, parts in blocks.items()})
    for row in data.records:
        if row["event"] == "provider_accepted" and row.get("response_sha256"):
            key = row["stage"], row["provider"]
            if key not in results or sha256(results[key].encode()).hexdigest() != row["response_sha256"]:
                raise RecoveryUnavailable(f"Accepted response body verification failed: {row['stage']}/{row['provider']}")
    return results


def _first(rows, event): return next((r for r in rows if r["event"] == event), None)


def latest_source(run_id):
    """Follow explicit recovery links so an old selection cannot repeat accepted work."""
    seen = set()
    identities = run_ids()
    while run_id not in seen:
        seen.add(run_id)
        children = []
        for candidate in identities:
            if candidate in seen: continue
            try: link = _first(read_run(candidate).records, "recovery_link")
            except (RecoveryUnavailable, OSError): continue
            if link and link.get("source_run_id") == run_id: children.append(candidate)
        if not children: return run_id
        run_id = max(children,key=run_sort)
    raise RecoveryUnavailable("Cyclic recovery history")


@dataclass
class Recipe:
    source_run_id: str
    root_run_id: str
    question: str
    profile: dict
    accepted: dict
    context_manifest: dict
    templates: dict
    references: dict

    @property
    def missing(self):
        from .workflow import LIVE_KEYS
        return [p for p in LIVE_KEYS if p not in self.accepted.get("individual", {}).get("REVISION", {})]

    @property
    def fingerprint(self):
        return sha256(json.dumps(dict(source=self.source_run_id, question=self.question, profile=self.profile,
                       context=self.context_manifest, templates=self.templates, references=self.references), sort_keys=True).encode()).hexdigest()

    def bundle(self):
        for rid in (self.source_run_id, self.root_run_id):
            cached = FROZEN_CONTEXTS.get(rid)
            if cached and json.loads(cached[1]) == self.context_manifest:
                return context_sources.ContextBundle(*cached)
        return context_sources.load_context(self.context_manifest)


def reconstruct(run_id, *, seen=None):
    from .workflow import (LIVE_KEYS, STAGES, prompt_templates, prepare_revision_inputs,
                           validate_messages, Message, revision_position, validate_profile)
    seen = set() if seen is None else set(seen)
    if run_id in seen or len(seen) >= 100: raise RecoveryUnavailable("Invalid recovery lineage")
    seen.add(run_id)
    data = read_run(run_id)
    if data.active: raise RecoveryUnavailable("This execution still has locally active work")
    rows, bodies = data.records, response_texts(data)
    started, link = _first(rows, "run_started"), _first(rows, "recovery_link")
    if not started: raise RecoveryUnavailable("Original run configuration is unavailable")
    validate_profile(started["profile"])
    stages = {r["stage"]: r for r in rows if r["event"] == "stage_accepted"}
    if link:
        recipe = reconstruct(link["source_run_id"], seen=seen)
        if link.get("reused_response_references") != recipe.references:
            raise RecoveryUnavailable("Recorded reused-response references disagree with original evidence")
        if started["profile"] != recipe.profile or started["question"] != recipe.question:
            raise RecoveryUnavailable("Recovery settings disagree with original inputs")
        recipe.source_run_id = run_id
    else:
        if not all(s in stages for s in STAGES[:2]):
            raise RecoveryUnavailable("INITIAL and CRITIQUE must both be accepted")
        context = _first(rows, "context_prepared")
        if not context: raise RecoveryUnavailable("Original context identities are unavailable")
        loaded = _first(rows, "implementation_loaded")
        templates = loaded.get("templates") if loaded else prompt_templates()
        if not loaded and sha256(json.dumps(templates,sort_keys=True).encode()).hexdigest() != LEGACY_TEMPLATE_SHA256:
            raise RecoveryUnavailable("No verified original template is available for this legacy run")
        if not isinstance(templates, dict) or set(templates) != set(STAGES):
            raise RecoveryUnavailable("Original stage templates are unavailable")
        accepted = dict(execution=started["profile"]["execution"], individual={"REVISION": {}},
                        council_name=started["profile"]["name"], usage=[], dispatched=[])
        question = started["question"]
        references = {}
        for stage in STAGES[:2]:
            results = stages[stage]["results"]
            if len(results) != 3: raise RecoveryUnavailable("Incomplete accepted stage metadata")
            payloads = []
            for result in results:
                provider = result["provider"]
                if (stage, provider) not in bodies: raise RecoveryUnavailable(f"Missing accepted {stage}/{provider} body")
                payload = dict(participant=provider, stage=stage, prompt=question, response=bodies[stage, provider])
                if stage == "CRITIQUE": payload["reviews"] = result["reviews"]
                payloads.append((provider, json.dumps(payload, sort_keys=True)))
                references[stage + ":" + provider] = dict(run_id=run_id, stage=stage, provider=provider,
                    sha256=sha256(payload["response"].encode()).hexdigest())
            accepted[stage] = tuple(payloads)
        initials = dict(accepted["INITIAL"])
        owners = {r["provider"]: r["proposal_id"] for r in stages["INITIAL"]["results"]}
        accepted["proposals"] = tuple((p, owners[p], json.loads(initials[p])["response"]) for p in LIVE_KEYS)
        provenance, inputs = [], {}
        for result in stages["CRITIQUE"]["results"]:
            reviewer, reviews = result["provider"], result["reviews"]
            by_id = {pid: body for _, pid, body in accepted["proposals"]}
            inputs[reviewer] = json.dumps(dict(stage="CRITIQUE", question=question,
                peers=[dict(**r, proposal=by_id[r["proposal_id"]]) for r in reviews]), sort_keys=True)
            provenance.extend((r["critique_id"], reviewer, r["proposal_id"], r["delivery_id"]) for r in reviews)
        accepted.update(provenance=tuple(provenance), critique_inputs=tuple(inputs.items()),
                        context_manifest=context["metadata"])
        validate_messages([Message("assistant", [t], author_name=p) for p,t in accepted["INITIAL"]],
                          question=question, response_bytes=accepted["execution"]["response_bytes"])
        prepare_revision_inputs(accepted, question=question)  # Validates the entire original routing graph.
        recipe = Recipe(run_id, run_id, question, started["profile"], accepted, context["metadata"], templates, references)
    accepted = recipe.accepted
    inputs = prepare_revision_inputs(accepted, question=recipe.question)
    individually_accepted = {r["provider"] for r in rows if r["event"] == "provider_accepted" and r.get("stage") == "REVISION"}
    if "REVISION" in stages: individually_accepted.update(r["participant"] for r in stages["REVISION"]["results"])
    for provider in individually_accepted:
        if provider not in LIVE_KEYS or ("REVISION", provider) not in bodies:
            raise RecoveryUnavailable("Missing accepted revision body/identity")
        if provider in accepted["individual"]["REVISION"]: continue
        body, request = bodies["REVISION", provider], json.loads(inputs[provider])
        payload = dict(participant=provider, stage="REVISION", prompt=recipe.question, response=body,
                       position=revision_position(body), origin_proposal_id=request["origin"]["proposal_id"],
                       feedback_references=[{k:r[k] for k in ("critique_id","delivery_id","proposal_id")}
                                            for r in request["feedback"]])
        accepted["individual"]["REVISION"][provider] = json.dumps(payload, sort_keys=True)
        recipe.references["REVISION:"+provider] = dict(run_id=run_id, stage="REVISION", provider=provider,
                                                     sha256=sha256(body.encode()).hexdigest())
    return recipe


def readiness(run_id):
    try:
        source = latest_source(run_id)
        recipe = reconstruct(source)
        return dict(eligible=bool(recipe.missing), source_run_id=source, root_run_id=recipe.root_run_id,
                    providers=recipe.missing, new_billable_attempts=len(recipe.missing), fingerprint=recipe.fingerprint,
                    reason="Fresh inference using original inputs; remote outcomes of canceled work remain unknown."
                           if recipe.missing else "All revisions are already accepted")
    except (RecoveryUnavailable, ValueError, KeyError, AssertionError, OSError, UnicodeError) as exc:
        return dict(eligible=False, providers=[], new_billable_attempts=0, reason=str(exc))
