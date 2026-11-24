"""Explicit, local-operator identity links between the old QQ memory and the
official runtime memory.

Design boundaries
-----------------
* Only mapping metadata is stored here (old source root/group/user and current
  runtime group/user, a monotonic revision, and an audit trail).  Old chat,
  notes, hashes and ids are never copied or rewritten.
* Links are bound to one old source root.  A numeric id is never auto-matched
  because it happens to be equal on the other side.
* Reads use a read-only SQLite connection; only ``ensure_schema`` and an
  explicit ``apply`` write to the mapping tables.
* The model/tool layer has no function that changes these links.  The only
  caller is the local memory console (``local_operator``).
"""
from __future__ import annotations


from hakimi.social.identity_base import GROUP_ID, IdentityError, LEGACY_MD_NOTE, LEGACY_RECORD_ID, LEGACY_REVIEW_GROUP, LEGACY_REVIEW_MEMBER, LINK_ACTIONS, MAX_BODY_FIELDS, REVISION, SCHEMA, USER_ID, _REQUEST_FIELDS, _clean_group, _clean_user, _old_root_id, _old_root_matches, _read_json, _runtime_path, _valid_group, _valid_user, old_root_key, old_root_label, source_root  # noqa: F401 (re-export)
from hakimi.social.identity_edit import _audit, _conflict, _member_conflict_message, _normalise_request, _preview_group_link, _preview_member_link, _preview_unlink, _preview_with_conn, apply, preview  # noqa: F401 (re-export)
from hakimi.social.identity_legacy import _RECALL_QUERY, _legacy_head, _legacy_label, _legacy_line, _legacy_origin, _legacy_tail, _legacy_text, _mapping_group, _mapping_label, _mapping_member, _md_note_raw, _note_relevant, _old_record_row, _operator_mapping_metadata, _page_bounds, _paginate, _query_terms, _read_md_note, _read_review_note, _recall_query, _review_note_raw, identity_history_for_person, read_for_operator, read_legacy, retrieve_legacy_notes  # noqa: F401 (re-export)
from hakimi.social.identity_new_catalog import _new_members_records_conn, _public_group_link, _public_member_link, _route_data, _runtime_group_stats, catalog, links_payload, new_group_catalog, new_groups_catalog, new_members_catalog  # noqa: F401 (re-export)
from hakimi.social.identity_old_catalog import _bias_nickname, _group_note_preview, _has_column, _md_group_headers, _md_notes, _old_columns, _old_conn, _old_group_stats, _old_members_records, _old_user_record_nickname, _review_data, _row_get, old_group_catalog, old_groups_catalog, old_members_catalog  # noqa: F401 (re-export)
from hakimi.social.identity_store import _columns, _group_link, _group_links, _member_link, _member_links, _open_read, _open_write, _revision_conn, _table_exists, ensure_schema, revision  # noqa: F401 (re-export)
