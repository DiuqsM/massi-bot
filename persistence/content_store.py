"""
Massi-Bot - Content Store

Queries for the content_catalog table in Supabase.
Maps between DB rows and the engine's ContentBundle / ContentTier types.

DB table: content_catalog
Columns:
  id, model_id, session_number, tier, bundle_id,
  fanvue_media_uuid, b2_key, media_type, price_cents, created_at

- tier is stored as INTEGER (1-6)
- price_cents is stored as INTEGER (e.g. $27.38 → 2738)
- bundle_id is the text identifier used by the script factory
"""

import logging
from typing import Optional, List, Dict, Any

from persistence.supabase_client import get_client

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'engine'))

from onboarding import ContentTier, TIER_CONFIG

logger = logging.getLogger(__name__)

TABLE = "content_catalog"

# Map ContentTier enum → integer position in the ladder
_TIER_TO_INT: Dict[ContentTier, int] = {
    ContentTier.TIER_1_BODY_TEASE: 1,
    ContentTier.TIER_2_TOP_TEASE: 2,
    ContentTier.TIER_3_TOP_REVEAL: 3,
    ContentTier.TIER_4_BOTTOM_REVEAL: 4,
    ContentTier.TIER_5_FULL_EXPLICIT: 5,
    ContentTier.TIER_6_CLIMAX: 6,
}

_INT_TO_TIER: Dict[int, ContentTier] = {v: k for k, v in _TIER_TO_INT.items()}


def _tier_to_int(tier: ContentTier) -> int:
    return _TIER_TO_INT[tier]


def _int_to_tier(n: int) -> Optional[ContentTier]:
    return _INT_TO_TIER.get(n)


def _row_to_bundle_info(row: Dict[str, Any]) -> Dict[str, Any]:
    """
    Convert a content_catalog DB row into a flat bundle info dict
    that the engine and connectors can use directly.
    """
    tier_enum = _int_to_tier(row.get("tier", 0))
    return {
        "db_id": str(row.get("id", "")),
        "bundle_id": row.get("bundle_id", ""),
        "model_id": str(row.get("model_id", "")),
        "session_number": row.get("session_number", 0),
        "tier": row.get("tier", 0),
        "tier_enum": tier_enum,
        "fanvue_media_uuid": row.get("fanvue_media_uuid"),
        "b2_key": row.get("b2_key"),
        "media_type": row.get("media_type", ""),
        # price_cents → dollars for engine consumption
        "price": (row.get("price_cents", 0) or 0) / 100.0,
        "price_cents": row.get("price_cents", 0),
        # Content description fields
        "bundle_context": row.get("bundle_context", ""),
        "clothing_description": row.get("clothing_description", ""),
        "location_description": row.get("location_description", ""),
        "mood": row.get("mood", ""),
        "tease_hint": row.get("tease_hint", ""),
        "key_detail": row.get("key_detail", ""),
    }


# ─────────────────────────────────────────────
# PUBLIC API
# ─────────────────────────────────────────────

def get_bundle_by_id(bundle_id: str, model_id: str) -> Optional[Dict[str, Any]]:
    """
    Look up a specific bundle by its bundle_id string and model_id.
    Returns the bundle info dict or None if not found.
    """
    db = get_client()
    result = (
        db.table(TABLE)
        .select("*")
        .eq("model_id", model_id)
        .eq("bundle_id", bundle_id)
        .single()
        .execute()
    )
    if not result.data:
        return None
    return _row_to_bundle_info(result.data)


def get_bundle_for_session_tier(
    model_id: str,
    session_number: int,
    tier: ContentTier,
) -> Optional[Dict[str, Any]]:
    """
    Look up the content bundle for a specific session + tier combination.
    Returns None if content isn't loaded yet for that slot.
    """
    db = get_client()
    result = (
        db.table(TABLE)
        .select("*")
        .eq("model_id", model_id)
        .eq("session_number", session_number)
        .eq("tier", _tier_to_int(tier))
        .limit(1)
        .execute()
    )
    if not result.data:
        return None
    return _row_to_bundle_info(result.data[0])


def get_available_bundle(
    model_id: str,
    tier: ContentTier,
    exclude_bundle_ids: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Find the next available bundle for a tier that hasn't been sent to this subscriber.
    Excludes bundle_ids already used. Falls back to the first available if all used.

    Returns bundle info dict or None if no content exists for this tier.
    """
    db = get_client()
    tier_int = _tier_to_int(tier)
    exclude = set(exclude_bundle_ids or [])

    result = (
        db.table(TABLE)
        .select("*")
        .eq("model_id", model_id)
        .eq("tier", tier_int)
        .order("session_number")
        .execute()
    )

    rows = result.data or []
    if not rows:
        logger.warning("No content for model %s tier %d", model_id, tier_int)
        return None

    # Pick first not in exclude list
    for row in rows:
        if row.get("bundle_id") not in exclude:
            return _row_to_bundle_info(row)

    # All bundles used — recycle the first one (least recently added)
    logger.debug(
        "All tier %d bundles used for model %s — recycling first",
        tier_int, model_id,
    )
    return _row_to_bundle_info(rows[0])


def get_model_catalog(model_id: str) -> Dict[int, List[Dict[str, Any]]]:
    """
    Load the full content catalog for a model, grouped by tier integer.
    Returns {1: [bundle_info, ...], 2: [...], ...}
    """
    db = get_client()
    result = (
        db.table(TABLE)
        .select("*")
        .eq("model_id", model_id)
        .order("tier")
        .order("session_number")
        .execute()
    )

    catalog: Dict[int, List[Dict[str, Any]]] = {t: [] for t in range(1, 7)}
    for row in (result.data or []):
        tier_int = row.get("tier", 0)
        if 1 <= tier_int <= 6:
            catalog[tier_int].append(_row_to_bundle_info(row))
    return catalog


def get_catalog_readiness(model_id: str, active_tier_count: int = 6) -> Dict[str, Any]:
    """
    Check how many bundles are loaded per tier for a model.
    Useful for /readiness command in the admin bot.
    Only checks tiers 1 through active_tier_count.
    """
    catalog = get_model_catalog(model_id)
    tiers = []
    ready = True
    for tier_int in range(1, active_tier_count + 1):
        tier_enum = _int_to_tier(tier_int)
        bundles = catalog.get(tier_int, [])
        config = TIER_CONFIG[tier_enum] if tier_enum else {}
        tiers.append({
            "tier": tier_int,
            "name": config.get("name", f"Tier {tier_int}"),
            "price": config.get("price", 0.0),
            "bundle_count": len(bundles),
            "has_fanvue_uuid": any(b.get("fanvue_media_uuid") for b in bundles),
        })
        if len(bundles) == 0:
            ready = False

    return {
        "model_id": model_id,
        "ready": ready,
        "tiers": tiers,
        "total_bundles": sum(t["bundle_count"] for t in tiers),
    }


def register_bundle(
    model_id: str,
    session_number: int,
    tier: ContentTier,
    bundle_id: str,
    media_type: str = "mixed",
    fanvue_media_uuid: Optional[str] = None,
    b2_key: Optional[str] = None,
    price_cents: Optional[int] = None,
    bundle_context: str = "",
    clothing_description: str = "",
    location_description: str = "",
    mood: str = "",
    tease_hint: str = "",
    key_detail: str = "",
) -> Dict[str, Any]:
    """
    Insert or update a content bundle entry in the catalog.
    price_cents defaults to the standard tier price if not provided.

    Returns the inserted/updated row as a bundle info dict.
    """
    db = get_client()
    tier_int = _tier_to_int(tier)

    if price_cents is None:
        price_cents = int(TIER_CONFIG[tier]["price"] * 100)

    row = {
        "model_id": model_id,
        "session_number": session_number,
        "tier": tier_int,
        "bundle_id": bundle_id,
        "media_type": media_type,
        "fanvue_media_uuid": fanvue_media_uuid,
        "b2_key": b2_key,
        "price_cents": price_cents,
        "bundle_context": bundle_context,
        "clothing_description": clothing_description,
        "location_description": location_description,
        "mood": mood,
        "tease_hint": tease_hint,
        "key_detail": key_detail,
    }

    result = (
        db.table(TABLE)
        .insert(row)
        .execute()
    )

    if not result.data:
        raise RuntimeError(
            f"Failed to register bundle {bundle_id} for model {model_id} "
            f"session {session_number} tier {tier_int}"
        )

    logger.info(
        "Registered bundle %s: model=%s session=%d tier=%d",
        bundle_id, model_id, session_number, tier_int,
    )
    return _row_to_bundle_info(result.data[0])


def update_fanvue_uuid(
    model_id: str,
    bundle_id: str,
    fanvue_media_uuid: str,
) -> None:
    """
    Set the Fanvue media UUID on an existing catalog entry.
    Called after uploading content to Fanvue.
    """
    db = get_client()
    db.table(TABLE).update(
        {"fanvue_media_uuid": fanvue_media_uuid}
    ).eq("model_id", model_id).eq("bundle_id", bundle_id).execute()
    logger.info("Updated fanvue_media_uuid for bundle %s", bundle_id)


def update_bundle_descriptions(
    model_id: str,
    bundle_id: str,
    bundle_context: str = "",
    clothing_description: str = "",
    location_description: str = "",
    mood: str = "",
    tease_hint: str = "",
    key_detail: str = "",
) -> None:
    """Update description fields on an existing content_catalog entry."""
    db = get_client()
    updates: Dict[str, str] = {}
    if bundle_context:
        updates["bundle_context"] = bundle_context
    if clothing_description:
        updates["clothing_description"] = clothing_description
    if location_description:
        updates["location_description"] = location_description
    if mood:
        updates["mood"] = mood
    if tease_hint:
        updates["tease_hint"] = tease_hint
    if key_detail:
        updates["key_detail"] = key_detail
    if updates:
        db.table(TABLE).update(updates).eq("model_id", model_id).eq("bundle_id", bundle_id).execute()
        logger.info("Updated descriptions for bundle %s (model %s)", bundle_id, model_id)
