"""Count errors in the denominator; keep classifier and final router separate."""
import math

from app.evaluation.intent_dataset import LABELS


def wilson95(successes: int, total: int) -> list[float] | None:
    if not total:
        return None
    z = 1.959963984540054
    proportion = successes / total
    denominator = 1 + z * z / total
    center = (proportion + z * z / (2 * total)) / denominator
    half = z * math.sqrt(proportion * (1 - proportion) / total + z * z / (4 * total * total)) / denominator
    return [max(0.0, center - half), min(1.0, center + half)]


def intent_metrics(rows: list[dict], prediction_key: str) -> dict:
    if not rows:
        raise ValueError('Cannot score empty intent results')
    confusion = {gold: {pred: 0 for pred in (*LABELS, 'ERROR')} for gold in LABELS}
    for row in rows:
        if row['expectedIntent'] not in LABELS:
            raise ValueError('Unknown gold intent')
        predicted = row.get(prediction_key)
        confusion[row['expectedIntent']][predicted if predicted in LABELS else 'ERROR'] += 1
    classes = {}
    for label in LABELS:
        tp = confusion[label][label]
        support = sum(confusion[label].values())
        predicted = sum(confusion[gold][label] for gold in LABELS)
        precision = tp / predicted if predicted else 0.0
        recall = tp / support if support else 0.0
        classes[label] = {'truePositive': tp, 'support': support, 'predicted': predicted,
                          'precision': precision, 'recall': recall,
                          'f1': 2 * precision * recall / (precision + recall) if precision + recall else 0.0,
                          'recallWilson95': wilson95(tp, support)}
    n = len(rows); correct = sum(confusion[label][label] for label in LABELS)
    risk = classes['RISK']; negatives = n - risk['support']
    false_positive = risk['predicted'] - risk['truePositive']
    return {'cases': n, 'correct': correct, 'accuracy': correct / n,
            'accuracyWilson95': wilson95(correct, n),
            'macroF1': sum(item['f1'] for item in classes.values()) / len(LABELS),
            'confusionMatrix': confusion, 'confusionOrientation': 'rows=gold, columns=prediction',
            'perClass': classes, 'riskRecall': risk['recall'], 'riskPrecision': risk['precision'],
            'riskFalseNegativeCount': risk['support'] - risk['truePositive'],
            'riskFalsePositiveCount': false_positive,
            'riskFalsePositiveRate': false_positive / negatives if negatives else None,
            'errorCount': sum(confusion[label]['ERROR'] for label in LABELS),
            'zeroDivision': 0,
            'intervalNote': 'Descriptive binomial Wilson intervals on this stratified synthetic set; '
                            'not population/clinical validity. Repeated runs do not create new independent cases.'}
