import math


def score_ranking(keys: list[str], qrels: dict[str, int], top_k: int) -> dict:
    if top_k <= 0:
        raise ValueError("top_k must be positive")
    relevant = {key: gain for key, gain in qrels.items() if gain > 0}
    seen = set()
    gains = []
    for key in keys[:top_k]:
        gains.append(relevant.get(key, 0) if key not in seen else 0)
        seen.add(key)
    if not relevant:
        return {"hit": False, "recallAtK": None, "precisionAtK": None,
                "reciprocalRank": None, "ndcgAtK": None, "firstRelevantRank": None,
                "retrievalEmpty": not keys}
    hits = sum(gain > 0 for gain in gains)
    first = next((i + 1 for i, gain in enumerate(gains) if gain > 0), 0)
    dcg = sum((2 ** gain - 1) / math.log2(i + 2) for i, gain in enumerate(gains))
    # IDCG uses ALL known relevant passages, including those not retrieved.
    ideal = sum((2 ** gain - 1) / math.log2(i + 2)
                for i, gain in enumerate(sorted(relevant.values(), reverse=True)[:top_k]))
    return {"hit": first > 0, "recallAtK": hits / len(relevant), "precisionAtK": hits / top_k,
            "reciprocalRank": 1 / first if first else 0.0, "ndcgAtK": dcg / ideal,
            "firstRelevantRank": first, "retrievalEmpty": not keys}


def aggregate(results: list[dict]) -> dict:
    positive = [row for row in results if row["recallAtK"] is not None]
    negative = [row for row in results if row["recallAtK"] is None]
    def mean(key):
        return sum(row[key] for row in positive) / len(positive) if positive else None
    return {"totalCases": len(results), "answerableCases": len(positive), "unanswerableCases": len(negative),
            "recallAtK": mean("recallAtK"), "precisionAtK": mean("precisionAtK"),
            "mrr": mean("reciprocalRank"), "ndcgAtK": mean("ndcgAtK"), "hitRate": mean("hit"),
            "unanswerableEmptyRetrievalRate": sum(row["retrievalEmpty"] for row in negative) / len(negative) if negative else None,
            "unanswerableNote": "Retrieval emptiness only; answer abstention/quality is a separate evaluation."}
