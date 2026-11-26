"""Historical flat module names, kept importable.

Before the package layout every module lived in one folder and was imported
by its bare name (``import memory_store``).  Code inside this package uses
package imports; this finder keeps old names working for scripts, tests and
operator tools.  Both names refer to the *same* module object.
"""
import importlib
import importlib.abc
import importlib.util
import sys

LAYOUT = {
    'core': (
        'runtime_io',
        'context_window',
        'time_tool',
        'host_secrets',
    ),
    'transport': (
        'official_transport',
        'official_files',
        'official_groups',
        'official_lane',
        'official_test',
        'official_waiter',
        'forward_messages',
        'member_mentions',
        'group_capabilities',
        'file_delivery',
        'image_permissions',
    ),
    'bridge': (
        'bridge_daemon',
        'conversation_scheduler',
        'conversation_judge',
        'turn_coordinator',
        'delivery_guard',
        'reply_format',
        'quote_policy',
        'participation_policy',
        'interaction_policy',
        'group_reply_quota',
        'study_recovery',
        'cold_topics',
        'wait_next_event',
        'bot_registry',
        'image_initiative',
    ),
    'prompt': (
        'prompt_context',
        'prompt_compaction',
        'prompt_language',
        'prompt_budget',
        'tool_catalog',
        'tool_selection',
    ),
    'models': (
        'vertex_route',
        'vertex_session',
        'scoped_session',
        'agy_session',
        'provider_health',
        'provider_control',
        'model_trial',
        'fallback_runner',
        'fallback_supervisor',
        'fallback_style',
        'fallback_media',
        'dsh_tools',
        'dsh_math',
        'dsh_text_judge',
    ),
    'tools': (
        'qq_cli',
        'bot_tools',
        'math_render',
        'bilibili_video',
        'bilibili_login',
        'genshin_version',
        'feeding',
        'sticker_catalog',
        'sticker_policy',
    ),
    'memory': (
        'memory_store',
        'memory_graph',
        'memory_lifecycle',
        'memory_recall',
        'memory_focus',
        'memory_usage',
        'memory_embedding_client',
        'memory_embedding_service',
        'memory_embedding_eval',
        'smart_memory',
        'legacy_md_notes',
        'impression_events',
        'personal_review',
        'repair_manual_scores',
    ),
    'social': (
        'memory_social',
        'social_behavior',
        'affinity_curve',
        'affinity_policy',
        'identity_links',
        'identity_member_context',
    ),
    'console': (
        'memory_console',
        'memory_graph_view',
    ),
    'features': (
        'diary_store',
        'diary_latex',
        'diary_daily',
        'qzone_publisher',
        'qzone_fallback',
        'codex_help',
        'codex_help_runner',
        'codex_help_service',
    ),
    'ops': (
        'cloud_service',
        'cloud_supervisor',
        'cloud_activity',
        'cloud_smoke',
        'terra_budget',
        'terra_supervision',
        'sol_repair',
        'collect_dsh_style',
        'verify_corpus',
    ),
}
LEGACY = {name: f"hakimi.{pkg}.{name}" for pkg, names in LAYOUT.items() for name in names}
LEGACY['vertex_web'] = 'hakimi.web.public_pages'

# Old dotted paths share the same module and state as their canonical imports.
# The executable web_fetch_worker keeps a file for direct invocations and python -m.
MODULE_ALIASES = {
    f"hakimi.models.{name}": f"hakimi.web.{name}"
    for name in (
        "anime_calendar", "dsh_search", "google_search", "search_budget",
        "search_judge", "search_policy", "search_routing",
        "web_access", "web_quality", "web_sources", "web_transport",
    )
}
MODULE_ALIASES['hakimi.models.vertex_web'] = 'hakimi.web.public_pages'


class _AliasLoader(importlib.abc.Loader):
    def __init__(self, target):
        self.target = target

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        # Replace the temporary alias without changing the real module's spec.
        sys.modules[module.__name__] = importlib.import_module(self.target)


class LegacyFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        dotted = LEGACY.get(name) or MODULE_ALIASES.get(name)
        if dotted is None:
            return None
        return importlib.util.spec_from_loader(name, _AliasLoader(dotted))


def install():
    if not any(isinstance(item, LegacyFinder) for item in sys.meta_path):
        # First, so a stray old copy (e.g. official_test_runtime/memory_store.py) is never imported.
        sys.meta_path.insert(0, LegacyFinder())
