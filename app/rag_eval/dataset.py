import hashlib
import json
from pathlib import Path

from app.services.knowledge import chunk_text


ROOT = Path(__file__).resolve().parents[2]


def load_dataset(path: str | Path, *, knowledge_root: Path | None = None) -> dict:
    path = Path(path)
    if not path.is_absolute():
        path = ROOT / path
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schemaVersion") != 2:
        raise ValueError("RAG evaluation requires schemaVersion=2 with explicit passage qrels; legacy source/term matches are not Recall@K")
    corpus = knowledge_root or ROOT / "app/knowledge"
    indexes = {}
    for source, digest in data["sources"].items():
        file = corpus / source
        if Path(source).name != source or hashlib.sha256(file.read_bytes()).hexdigest() != digest:
            raise ValueError(f"Frozen corpus changed: {source}; create a new labeled dataset version")
        indexes[source] = len(chunk_text(file.read_text(), data["chunking"]["size"], data["chunking"]["overlap"]))
    ids = set()
    for case in data["queries"]:
        if case["id"] in ids or bool(case["qrels"]) != case["answerable"]:
            raise ValueError("Invalid query identity or answerability")
        ids.add(case["id"])
        gold_keys = set()
        for qrel in case["qrels"]:
            key = (qrel["source"], qrel["sourceIndex"])
            if (key in gold_keys or qrel["source"] not in indexes
                    or type(qrel["sourceIndex"]) is not int
                    or not 0 <= qrel["sourceIndex"] < indexes[qrel["source"]]
                    or type(qrel["gain"]) is not int or qrel["gain"] not in {1, 2}):
                raise ValueError(f"Invalid qrel in {case['id']}")
            gold_keys.add(key)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = json.loads((ROOT / "app/evaluation/data/frozen-manifest.json").read_text())
    try:
        expected = manifest.get(path.resolve().relative_to(ROOT).as_posix())
    except ValueError:
        expected = None
    if expected is not None and expected != digest:
        raise ValueError("Frozen dataset changed; create a new version")
    data["datasetSha256"] = digest
    data["frozenVerified"] = expected == digest
    return data
