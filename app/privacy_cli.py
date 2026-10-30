"""Trusted local operator commands; never exposed as web/MCP model tools."""
import argparse
import json
import os


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='command',required=True)
    retention=commands.add_parser('retention');mode=retention.add_mutually_exclusive_group();mode.add_argument('--apply',action='store_true');mode.add_argument('--dry-run',action='store_true');retention.add_argument('--offline',action='store_true')
    for name in ['grant','revoke']:
        role=commands.add_parser(name);role.add_argument('--username',required=True)
        role.add_argument('--role',choices=['sensitive-reader','privacy-auditor'],required=True)
    args=parser.parse_args(argv)
    if args.command=='retention' and args.apply and not args.offline:
        parser.error('Applying content erasure requires --offline after stopping application writers; default is preview.')
    from app.core.bootstrap import create_schema
    from app.core.config import get_settings
    from app.core.database import SessionLocal
    from app.models.entities import DataAccessAudit,UserAccount
    create_schema()
    with SessionLocal() as db:
        if args.command=='retention':
            from app.services.data_retention import DataRetentionService
            result=DataRetentionService(db,get_settings()).run(apply=args.apply)
        else:
            user=db.query(UserAccount).filter_by(username=args.username).first()
            if user is None or 'ROLE_ADMIN' not in user.roles:
                parser.error('Target must be an existing administrator')
            role='ROLE_SENSITIVE_READER' if args.role=='sensitive-reader' else 'ROLE_PRIVACY_AUDITOR'
            roles=set(user.roles)
            if args.command=='grant':roles.add(role)
            else:roles.discard(role)
            user.roles=roles
            db.add(DataAccessAudit(action='ROLE_'+args.command.upper(),policy=role,operator_uid=os.getuid(),resource='account',resource_id=str(user.id),purpose='',status='ALLOWED',row_count=1))
            db.commit();result={'accountId':user.id,'role':role,'operation':args.command,'applied':True}
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
