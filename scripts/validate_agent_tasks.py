"""Validate frozen task *data* only. Never imports or executes the application."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / 'app/evaluation/data/agent-tasks-v1'


def require(condition, message):
    if not condition:
        raise ValueError(message)


def validate_contracts(data: dict, schema: dict) -> dict:
    Draft202012Validator.check_schema(schema)
    errors = list(Draft202012Validator(schema).iter_errors(data))
    if errors:
        e = errors[0]
        raise ValueError(f'Schema error at {"/".join(map(str, e.absolute_path))}: {e.message}')
    tasks = data['tasks']
    require(len({t['id'] for t in tasks}) == len(tasks), 'Duplicate task ID')
    require(len({t['title'] for t in tasks}) == len(tasks), 'Duplicate task title')
    families, inputs = {}, {}
    tools = set(data['toolCatalog'])
    actors = set(data['actorCatalog'])
    policies = {'LOW': {'EXCEL_REPORT'}, 'MEDIUM': {'EXCEL_REPORT', 'CASE_CREATE'},
                'HIGH': {'EXCEL_REPORT', 'CASE_CREATE', 'ALERT_SEND'}}
    for t in tasks:
        tag = t['id']
        family, split = t['familyId'], t['split']
        require(families.setdefault(family, split) == split, f'{tag}: family leakage across splits')
        require(set(t['initialState']['actorAliases']) <= actors, f'{tag}: unknown actor')
        sessions = {s['alias']: s for s in t['initialState']['sessions']}
        require(len(sessions) == len(t['initialState']['sessions']), f'{tag}: duplicate session alias')
        require(all(s['owner'] in actors for s in sessions.values()), f'{tag}: session owner missing')
        report_aliases = {r['alias'] for r in t['initialState']['reports']}
        job_aliases = {j['alias'] for j in t['initialState']['jobs']}
        require(len(report_aliases) == len(t['initialState']['reports']), f'{tag}: duplicate report alias')
        require(len(job_aliases) == len(t['initialState']['jobs']), f'{tag}: duplicate job alias')
        require(all(r['session'] in sessions for r in t['initialState']['reports']), f'{tag}: report session missing')
        require(all(j['report'] in report_aliases for j in t['initialState']['jobs']), f'{tag}: job report missing')
        allowed, forbidden = set(t['allowedToolKinds']), set(t['prohibitedToolKinds'])
        require(not allowed & forbidden and allowed | forbidden == tools, f'{tag}: tool permission partition conflict')
        require(set(t['hardConstraints']) <= set(data['hardConstraintCatalog']), f'{tag}: undefined hard constraint')
        require(len({c['id'] for c in t['successCriteria']}) == len(t['successCriteria']), f'{tag}: duplicate criterion ID')
        require(all(c['id'].startswith(tag+'.') for c in t['successCriteria']), f'{tag}: wrong criterion binding')
        kinds = {c['kind'] for c in t['successCriteria']}
        if t['executionScope'] == 'conversation':
            require(bool(t['turns']) and not t['boundaryActions'], f'{tag}: conversation needs turns only')
            require({'route_matches_contract','report_tool_contract','terminal_effects_match','reply_rubric'} <= kinds,
                    f'{tag}: missing conversation completion criteria')
            require({'no_unreviewed_candidate','no_fabricated_completion','current_turn_bindings','reply_safety_boundaries'} <= set(t['hardConstraints']),
                    f'{tag}: missing output/provenance hard constraints')
        else:
            require(not t['turns'] and bool(t['boundaryActions']), f'{tag}: boundary needs actions only')
            require(t['eligibleModes'] == ['boundary_controlled'], f'{tag}: boundary probe cannot claim real-model trials')
            require(t['expectedOutcome']['handling'] == 'deny' and t['expectedOutcome']['goalStatus'] == 'blocked',
                    f'{tag}: boundary rejection must not be business goal completion')
        all_required = set()
        require(len({x['id'] for x in t['turns']}) == len(t['turns']), f'{tag}: duplicate turn ID')
        for turn in t['turns']:
            require(turn['actor'] in actors and turn['session'] in sessions, f'{tag}: dangling turn reference')
            # Exact duplicates within a task may intentionally test repeated high
            # risk. Reuse across task partitions is not allowed.
            normalized = ''.join(turn['input'].lower().split())
            require(inputs.setdefault(normalized, split) == split, f'{tag}: duplicate input across splits')
            e = turn['expected']
            intents, risks = set(e['acceptedIntents']), set(e['acceptedRisks'])
            required = {r['kind'] for r in e['requiredTools']}
            require(len(required) == len(e['requiredTools']), f'{tag}: duplicate required tool')
            require(not required & set(e['forbiddenTools']) and required | set(e['forbiddenTools']) == tools,
                    f'{tag}: turn tool contract conflict')
            all_required |= required
            foreign = sessions[turn['session']]['owner'] != turn['actor']
            if foreign:
                require(e['replyMode'] == 'access_error' and e['donePolicy'] == 'forbidden',
                        f'{tag}: foreign session must reject, never complete a normal reply')
                require(not intents and not risks and not required and e['reportPolicy'] == 'forbidden',
                        f'{tag}: foreign session must not invent assessment or report')
                require('foreign_session_denied' in kinds, f'{tag}: ownership denial criterion missing')
                continue
            require(bool(intents) and bool(risks), f'{tag}: normal turn needs route labels')
            require(len(risks) == 1, f'{tag}: v1 risk rubric must choose an explicit operational level')
            risk = next(iter(risks))
            if risk == 'HIGH':
                require(intents == {'RISK'} and e['reportPolicy'] == 'required', f'{tag}: HIGH cannot lose report')
            if risk == 'MEDIUM':
                require(intents == {'CONSULT'} and e['reportPolicy'] == 'required', f'{tag}: MEDIUM route mismatch')
            if e['reportPolicy'] == 'required':
                require(required == policies[risk] and intents != {'CHAT'}, f'{tag}: risk/tool contract mismatch')
            elif e['reportPolicy'] == 'if_consult_or_risk':
                require(risk == 'LOW' and intents == {'CHAT','CONSULT'} and required == policies['LOW'],
                        f'{tag}: ambiguous route needs explicit conditional LOW report/tools')
            else:
                require(not required, f'{tag}: forbidden/aborted report cannot have required effects')
            if intents == {'CHAT'}:
                require(risk == 'LOW' and e['reportPolicy'] == 'forbidden', f'{tag}: CHAT unexpectedly creates report')
            if e['replyMode'] in {'safe_fallback','no_reply_after_disconnect','access_error'}:
                require(e['donePolicy'] == 'forbidden', f'{tag}: fallback/disconnect must not signal done')
            for requirement in e['requiredTools']:
                if requirement['terminalState'] == 'DEAD':
                    require(requirement['kind'] == 'ALERT_SEND' and any(
                        f['stage'] == 'tool.ALERT_SEND' and f['parameters'].get('times') == 'all'
                        for f in t['environment']['faults']), f'{tag}: DEAD needs a permanent failure fixture')
        require(all_required == allowed, f'{tag}: task/turn allowed tool union mismatch')
        refs = actors | set(sessions) | report_aliases | job_aliases
        for action in t['boundaryActions']:
            require(action['actor'] in actors, f'{tag}: boundary actor missing')
            for k, value in action['arguments'].items():
                if k.endswith('Ref'):
                    require(isinstance(value,str) and value.split('.')[0] in refs, f'{tag}: dangling symbolic {k}')
        fault_ids = [f['id'] for f in t['environment']['faults']]
        require(len(set(fault_ids)) == len(fault_ids), f'{tag}: duplicate fault ID')
        observed = [c['parameters'].get('faultId') for c in t['successCriteria'] if c['kind']=='fault_trigger_observed']
        require(set(observed) == set(fault_ids) and len(observed) == len(fault_ids), f'{tag}: unverified fault trigger')
        deadline = t['environment']['configurationOverrides'].get('turn_timeout_seconds')
        require(deadline is None or deadline == t['completionWindows']['perTurnSeconds'], f'{tag}: deadline contract mismatch')
    require(Counter(t['category'] for t in tasks) == {k:10 for k in data['categories']}, 'Category coverage must be 6 x 10')
    require(Counter(t['split'] for t in tasks) == {'development':40,'holdout':20}, 'Split must remain 40 / 20')
    return {'tasks':len(tasks), 'turns':sum(len(t['turns']) for t in tasks),
      'splits':dict(Counter(t['split'] for t in tasks)),
      'categories':dict(Counter(t['category'] for t in tasks)),
      'executionScopes':dict(Counter(t['executionScope'] for t in tasks)),
      'families':len(families), 'faultScenarios':sum(bool(t['environment']['faults']) for t in tasks),
      'contractStatus':data['status'], 'evaluationExecuted':False}


def validate_dataset(directory: Path = DIRECTORY, root: Path = ROOT):
    manifest = json.loads((directory/'manifest.json').read_text())
    for section in ('artifactHashes','corpusHashes','legacyDatasetHashes'):
        for relative, expected in manifest[section].items():
            path = (root/relative).resolve()
            require(path.is_relative_to(root.resolve()), 'Manifest path escapes repository')
            require(path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == expected,
                    f'Frozen hash mismatch: {relative}')
    data = json.loads((directory/'tasks.json').read_text())
    schema = json.loads((directory/'schema.json').read_text())
    summary = validate_contracts(data,schema)
    require(manifest['datasetVersion'] == data['version'], 'Manifest version mismatch')
    summary.update(frozenVerified=True, businessCodeExecuted=False, modelApiRequests=0)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',type=Path,default=DIRECTORY)
    args=parser.parse_args()
    try:
        print(json.dumps(validate_dataset(args.directory),ensure_ascii=False,indent=2))
    except (OSError,ValueError,KeyError) as error:
        print(f'INVALID: {error}',file=sys.stderr)
        return 1
    return 0


if __name__=='__main__':
    raise SystemExit(main())
