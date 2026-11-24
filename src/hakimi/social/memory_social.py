"""Provenance-separated social impressions and observable interaction familiarity.

Three different things are deliberately kept apart here:

* ``memory_originals`` rows authored by Gemini are the only "handwritten" text;
* ``social_assessments`` with ``provider='dsh'`` are the owner-authorised,
  DSH-assisted reading of public evidence (never presented as Gemini text);
* direct ``@``/reply statistics are observations, not a liking score.

No model or network call is made by this module.  The import CLI is explicit;
importing the module never writes to a database.
"""
from __future__ import annotations

import argparse
import json
from hakimi.social.social_base import CQ_AT, CQ_REPLY, PERSON_ID, PROVENANCE_LABELS, QUOTE_STARTS, TERRA_PROVENANCE, _assessment_dict, _current_text, _direct_targets, _evidence_involves, _evidence_text_available, _reply_target, _row_targets, _stored_affinity_policy, _text_targets, _utc_day  # noqa: F401 (re-export)
from hakimi.social.social_schema import SCHEMA_INDEXES, SCHEMA_TABLE, _create_indexes, _quote, _rebuild_for_terra, _release_savepoint, _rollback_savepoint, _savepoint, _table_sql, _unique_migration_table, ensure_schema  # noqa: F401 (re-export)
from hakimi.social.social_fields import DEFAULT_PROVENANCE, HAKIMI_SUBJECT, MAX_EVIDENCE, PROVENANCES, SCHEMA, SEED_PROVENANCE, SESSIONS, SocialError, _append_impression_text, _assessment_evidence_ids, _assessment_scope, _clean_text, _evidence_ids, _existing, _find_manual_sources, _find_prior_impression, _inheritance_eligible, _inheritance_metadata, _latest_active, _load_visible_evidence, _manual_source, _merge_inherited_evidence, _request_id, _same_revision, _scope, _score, _stable_pair_key, _valid_evidence_rows, _with_inheritance_policy, validate_fields  # noqa: F401 (re-export)
from hakimi.social.social_record import _duplicate_receipt, append_social, record_social  # noqa: F401 (re-export)
from hakimi.social.social_repair import repair_manual_scores  # noqa: F401 (re-export)
from hakimi.social.social_query import assessment_detail, assessment_evidence, latest_assessments, latest_by_direction, person_social_relations  # noqa: F401 (re-export)
from hakimi.social.interaction_familiarity import familiar_score, interaction_stats, pair_familiarity, pair_stats  # noqa: F401 (re-export)
from hakimi.social.social_seed import _read_json, _seed_items, import_seed, validate_seed  # noqa: F401 (re-export)


def main(argv=None):
    parser = argparse.ArgumentParser(description="辅助关系分析种子：显式验证与导入（不调用模型）")
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate-seed", help="只检查种子结构与证据M编号，不写数据库")
    validate.add_argument("--seed", required=True)
    validate.add_argument("--evidence", default="")
    imp = sub.add_parser("import-seed", help="把dsh_assisted种子幂等导入本地记忆库")
    imp.add_argument("--base", required=True)
    imp.add_argument("--seed", required=True)
    imp.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "validate-seed":
        result = validate_seed(args.seed, args.evidence or None)
    else:
        result = import_seed(args.base, args.seed, dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
