"""
Massi-Bot Bot Engine - Data Models
Defines subscriber profiles, persona configurations, script structures,
and all data types used throughout the conversation engine.
"""

from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Any
from datetime import datetime, timedelta
import json
import uuid


# ─────────────────────────────────────────────
# ENUMS - Pipeline States & Classifications
# ─────────────────────────────────────────────

class SubState(Enum):
    """Subscriber pipeline states (the core state machine)."""
    NEW = "new"                         # Just subscribed, no messages yet
    WELCOME_SENT = "welcome_sent"       # Welcome message sent, awaiting reply
    QUALIFYING = "qualifying"           # Asking qualifying questions
    GFE_BUILDING = "gfe_building"       # Relationship-building before selling (GFE-first)
    SEXT_CONSENT = "sext_consent"       # Consent gate checkpoint
    CLASSIFIED = "classified"           # Sub type determined, ready to route
    WARMING = "warming"                 # Building rapport, sex-adjacent talk
    TENSION_BUILD = "tension_build"     # Sub engaged, building toward first PPV
    FIRST_PPV_READY = "first_ppv_ready" # Ready to drop first PPV
    FIRST_PPV_SENT = "first_ppv_sent"   # First PPV sent, awaiting response
    LOOPING = "looping"                 # In sell-dirty_talk-sell loop
    GFE_ACTIVE = "gfe_active"           # Full GFE mode, emotional bonding
    CUSTOM_PITCH = "custom_pitch"       # Pitching custom content
    POST_SESSION = "post_session"       # Post-nut care / session wind-down
    RETENTION = "retention"             # Long-term retention rhythm
    RE_ENGAGEMENT = "re_engagement"     # Re-engaging after ghost
    COOLED_OFF = "cooled_off"           # Sub went quiet, waiting
    DISQUALIFIED = "disqualified"       # Timewaster, deprioritized


class SubType(Enum):
    """Subscriber classification types (from Doc 3)."""
    UNKNOWN = "unknown"
    HORNY = "horny"           # Already turned on, wants to buy fast
    ATTRACTED = "attracted"   # Likes the model, not fully sold yet
    CURIOUS = "curious"       # Interested in non-sexual topic
    TIMEWASTER = "timewaster" # Just wants attention or free content
    WHALE = "whale"           # High-value, repeat buyer


class SubTier(Enum):
    """Spending tier for pricing decisions."""
    UNPROVEN = "unproven"     # No purchases yet
    LOW = "low"               # $1-$25 total spend
    MID = "mid"               # $25-$100 total spend
    HIGH = "high"             # $100-$500 total spend
    WHALE = "whale"           # $500+ total spend


class ScriptPhase(Enum):
    """Phases within a script arc."""
    INTRO = "intro"             # Setup / context
    TEASE = "tease"             # Flirty warm-up
    HEAT_BUILD = "heat_build"   # Dirty talk escalation
    PPV_DROP = "ppv_drop"       # Content send moment
    REACTION = "reaction"       # Post-unlock engagement
    ESCALATION = "escalation"   # Next level tease
    CUSTOM_TEASE = "custom_tease"  # Custom content pitch
    COOLDOWN = "cooldown"       # Post-session GFE care


class ObjectionType(Enum):
    """Common objection categories (from Doc 6)."""
    TOO_EXPENSIVE = "too_expensive"
    WANTS_CHEAPER = "wants_cheaper"
    MAYBE_LATER = "maybe_later"
    SPENT_TOO_MUCH = "spent_too_much"
    GHOSTING = "ghosting"
    WANTS_FREE = "wants_free"
    WANTS_MEETUP = "wants_meetup"


class NicheType(Enum):
    """Model niche / persona categories."""
    FITNESS = "fitness"
    GAMER = "gamer"
    EGIRL = "egirl"
    LATINA = "latina"
    MILF = "milf"
    GIRL_NEXT_DOOR = "girl_next_door"
    BADDIE = "baddie"
    NERDY = "nerdy"
    PARTY_GIRL = "party_girl"
    CUSTOM = "custom"


# ─────────────────────────────────────────────
# PERSONA - Model Voice & Identity Config
# ─────────────────────────────────────────────

@dataclass
class PersonaVoice:
    """
    Model Voice Checklist (from Doc 2, Section 6).
    Defines how the bot speaks for a given persona/niche.
    """
    primary_tone: str = "flirty & sweet"       # e.g., "sarcastic baddie", "chill gamer"
    emoji_use: str = "moderate"                 # "heavy", "light", "none"
    swear_words: str = "rarely"                 # "yes", "no", "rarely"
    slang_style: str = "gen_z"                  # "gen_z", "latina", "milf_formal", "egirl"
    flirt_style: str = "playful"                # "direct", "innocent", "power_play", "playful"
    favorite_phrases: List[str] = field(default_factory=lambda: [
        "stop it 😩", "I can't with you", "you're trouble"
    ])
    sexual_escalation_pace: str = "slow_burn"   # "fast", "slow_burn", "gfe_only"
    reaction_phrases: List[str] = field(default_factory=lambda: [
        "ugh I can't", "omg stahppp", "you're literally the worst"
    ])
    greeting_style: str = "casual"              # "casual", "excited", "mysterious", "bold"
    message_length: str = "short"               # "short", "medium", "long"
    capitalization: str = "lowercase_casual"     # "proper", "lowercase_casual", "mixed"
    punctuation_style: str = "minimal"          # "proper", "minimal", "dramatic"


@dataclass
class Persona:
    """
    Full persona configuration for one Instagram account / model niche.
    Each IG account maps to one Persona with its own voice, scripts, and pricing.
    """
    persona_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    name: str = ""
    nickname: str = ""
    niche: NicheType = NicheType.GIRL_NEXT_DOOR
    ig_account_tag: str = ""           # Maps to IG account for attribution
    ig_account_url: str = ""
    location_story: str = ""           # Where "she's" from
    age: int = 22
    hobbies: List[str] = field(default_factory=list)
    favorite_shows: List[str] = field(default_factory=list)
    favorite_foods: List[str] = field(default_factory=list)
    voice: PersonaVoice = field(default_factory=PersonaVoice)
    sexual_boundaries: List[str] = field(default_factory=list)  # What she won't do
    available_content_types: List[str] = field(default_factory=lambda: [
        "tease_pic", "reveal_pic", "tease_video", "toy_video", "climax_video"
    ])
    # Pricing tiers for this persona
    pricing: Dict[str, Dict[str, float]] = field(default_factory=lambda: {
        "light_tease": {"min": 5, "max": 10},
        "reveal": {"min": 15, "max": 25},
        "toy_climax": {"min": 25, "max": 40},
        "full_explicit": {"min": 40, "max": 65},
        "custom": {"min": 100, "max": 500},
    })
    # Niche-specific identifiers for attribution detection
    niche_keywords: List[str] = field(default_factory=list)
    niche_topics: List[str] = field(default_factory=list)


# ─────────────────────────────────────────────
# SUBSCRIBER - Profile & Tracking
# ─────────────────────────────────────────────

@dataclass
class QualifyingData:
    """Data gathered during qualification phase."""
    age: Optional[int] = None
    location: Optional[str] = None
    occupation: Optional[str] = None
    relationship_status: Optional[str] = None
    subscribe_reason: Optional[str] = None
    interests: List[str] = field(default_factory=list)
    # Whale indicators
    mentions_spending: bool = False
    emotional_openness: int = 0          # 0-10 scale
    response_speed: str = "normal"       # "instant", "normal", "slow"
    message_length: str = "normal"       # "one_word", "normal", "paragraph"
    initiated_sexual: bool = False


@dataclass
class SpendingHistory:
    """Tracks subscriber spending behavior."""
    total_spent: float = 0.0
    ppv_count: int = 0
    custom_count: int = 0
    tip_count: int = 0
    last_purchase_date: Optional[datetime] = None
    avg_ppv_price: float = 0.0
    highest_single_purchase: float = 0.0
    rejected_ppv_count: int = 0
    price_objection_count: int = 0

    @property
    def tier(self) -> SubTier:
        if self.total_spent == 0:
            return SubTier.UNPROVEN
        elif self.total_spent < 25:
            return SubTier.LOW
        elif self.total_spent < 100:
            return SubTier.MID
        elif self.total_spent < 500:
            return SubTier.HIGH
        else:
            return SubTier.WHALE

    @property
    def is_buyer(self) -> bool:
        return self.ppv_count > 0

    @property
    def conversion_rate(self) -> float:
        total_attempts = self.ppv_count + self.rejected_ppv_count
        if total_attempts == 0:
            return 0.0
        return self.ppv_count / total_attempts


@dataclass
class Subscriber:
    """
    Full subscriber profile - the 'Whale Log' entry (from Doc 5, Section 6).
    Tracks everything needed to personalize conversations and maximize LTV.
    """
    sub_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    username: str = ""
    display_name: str = ""

    # Pipeline state
    state: SubState = SubState.NEW
    sub_type: SubType = SubType.UNKNOWN
    persona_id: str = ""                # Which persona/niche they're assigned to

    # Attribution
    source_ig_account: str = ""         # Which IG account they came from
    source_detected: bool = False
    subscribe_date: datetime = field(default_factory=datetime.now)

    # Qualifying data
    qualifying: QualifyingData = field(default_factory=QualifyingData)

    # Spending
    spending: SpendingHistory = field(default_factory=SpendingHistory)

    # Conversation tracking
    message_count: int = 0
    qualifying_questions_asked: int = 0
    current_script_id: Optional[str] = None
    current_script_phase: Optional[ScriptPhase] = None
    current_loop_number: int = 0        # Which PPV loop we're on
    scripts_completed: List[str] = field(default_factory=list)

    # GFE tracking
    gfe_active: bool = False
    personal_details_shared: Dict[str, str] = field(default_factory=dict)
    callback_references: List[str] = field(default_factory=list)  # Things to reference later
    emotional_hooks: List[str] = field(default_factory=list)       # What resonated

    # Engagement signals
    last_message_date: Optional[datetime] = None
    last_active_date: Optional[datetime] = None
    ghost_count: int = 0
    re_engagement_attempts: int = 0

    # Red flags
    asked_for_meetup: bool = False
    asked_for_free_content: int = 0
    one_word_reply_streak: int = 0
    abusive: bool = False

    # Session control (v2.1)
    tier_no_count: int = 0                    # Objections to current tier (resets on purchase)
    last_session_completed_at: Optional[datetime] = None  # When last full session ended
    session_locked_until: Optional[datetime] = None       # No new sessions before this time
    custom_declined: bool = False             # Declined the custom pitch this session
    brokey_flagged: bool = False              # Hit 3 nos, got the brokey treatment
    last_pitch_at: Optional[datetime] = None  # Last time a PPV was pitched (same-day dedup)
    sent_captions: List[str] = field(default_factory=list)  # PPV captions already used this session (dedup)

    # GFE-first tracking
    gfe_message_count: int = 0              # Fan messages received during GFE_BUILDING phase
    sext_consent_given: bool = False         # Legacy flag — kept for backwards compat; gate now uses horniness_score
    horniness_score: int = 0                 # 0-10, updated every message by Opus. Grok kicks in at > 5.
    fan_name: str = ""                       # Preferred name/nickname extracted by agent ("Jake", "daddy", etc.)
    fan_profile: Dict[str, Any] = field(default_factory=lambda: {
        "personality": "",   # how he communicates and behaves (e.g. "shy but bold when comfortable")
        "interests": [],     # hobbies and topics he brings up
        "kinks": [],         # what turns him on
        "notes": "",         # anything else worth remembering
    })
    tags: List[str] = field(default_factory=list)  # Free-form admin labels e.g. ["vip", "shy", "price-sensitive"]
    gfe_continuation_pending: bool = False   # Waiting for $20 continuation payment
    gfe_continuations_paid: int = 0          # How many continuation fees they've paid

    # PPV realness (Cobalt-Strike jitter + heads-up tracking)
    ppv_heads_up_count: int = 0              # How many "give me a few minutes" pre-PPV messages sent
    ppv_threshold_jitter: Optional[int] = None  # Randomized msgs-before-first-PPV (8-14), set once per session
    last_consent_decline_at_msg_count: Optional[int] = None  # gfe_message_count when fan declined to spend

    # Pending PPV (for 6h auto-delete of unpaid tier drops)
    # dict: {platform_msg_id: str, tier: int, sent_at: iso_str, bundle_id: str, price: float}
    pending_ppv: Optional[Dict[str, Any]] = None

    # Multi-session flow — stays at current session until tier 6 paid (advances) or GFE-kick (stays)
    current_session_number: int = 1

    # Custom request pushback streak (3 consecutive off-ladder asks → allow custom_pitch)
    custom_request_streak: int = 0

    # Goodbye pattern tracking — learns each fan's departure/return behavior across the relationship
    # Each entry: {at: iso_str, tier_pending: int, returned_at: iso_str|None, opened_ppv_on_return: bool}
    goodbye_patterns: List[Dict[str, Any]] = field(default_factory=list)

    # In-flight departure: set when fan signals "gotta go", cleared when they return
    # Format: {at: iso_str, tier_pending: int|None}
    in_flight_departure: Optional[Dict[str, Any]] = None

    # Continuation paywall jitter — re-randomized each cycle (25-35 messages)
    continuation_threshold_jitter: Optional[int] = None

    # Pending custom order (OUT OF BAND from tier ladder)
    # Shape: {request_text, custom_type, quoted_price, pitched_at, fan_confirmed_paid_at,
    #         admin_confirmed_at, admin_last_alerted_at, status}
    # Status: pitched | awaiting_admin_confirm | paid | denied | fulfilled
    pending_custom_order: Optional[Dict[str, Any]] = None

    # High-value utterance registry (anti-repetition for critical messages)
    # Keys: category names (money_readiness_ask, ppv_heads_up, etc.)
    # Values: list of full bot messages sent in that category (FIFO, max 30 each)
    high_value_utterances: Dict[str, List[str]] = field(default_factory=dict)
    # Archive: evicted entries stored as {category: [{hash, ts}]} for future A/B analysis
    high_value_utterances_archive: Dict[str, List[Dict[str, str]]] = field(default_factory=dict)

    # Crash recovery
    last_crash_time: Optional[datetime] = None  # Set when pipeline crashes; cleared on next success

    # Error / auto-recovery state (Fix 11 — every-error Telegram alerts + recovery sweep)
    # All timestamps are ISO-8601 strings stored in qualifying_data JSONB.
    last_error_at: Optional[str] = None              # ISO, set on pipeline error, cleared on recovery
    last_error_context: Optional[Dict[str, Any]] = None   # {operation, error_type, error_msg, tb_snippet}
    last_successful_bot_message_at: Optional[str] = None  # ISO, stamped on every successful send
    unrecovered_inbound: List[Dict[str, Any]] = field(default_factory=list)  # [{text, received_at}]
    recovery_attempts: int = 0
    recovery_next_attempt_at: Optional[str] = None   # ISO, next scheduled retry per backoff
    recovery_manual_only: bool = False               # True for purchase-path errors — never auto-retried

    # Conversation history (last N messages for context)
    recent_messages: List[Dict[str, str]] = field(default_factory=list)

    @property
    def days_since_subscribe(self) -> int:
        return (datetime.now() - self.subscribe_date).days

    @property
    def days_since_last_message(self) -> Optional[int]:
        if self.last_message_date:
            return (datetime.now() - self.last_message_date).days
        return None

    @property
    def is_ghost(self) -> bool:
        if self.days_since_last_message is None:
            return False
        return self.days_since_last_message >= 2

    @property
    def whale_score(self) -> int:
        """0-100 score indicating whale potential."""
        score = 0
        # Age factor (older = higher)
        if self.qualifying.age and self.qualifying.age >= 35:
            score += 15
        elif self.qualifying.age and self.qualifying.age >= 28:
            score += 10
        # Occupation factor
        high_income_keywords = ["engineer", "doctor", "lawyer", "manager",
                                "director", "owner", "exec", "finance", "tech"]
        if self.qualifying.occupation:
            if any(k in self.qualifying.occupation.lower() for k in high_income_keywords):
                score += 20
        # Relationship status (lonely = better for GFE)
        if self.qualifying.relationship_status in ["single", "divorced", "separated"]:
            score += 15
        # Spending history
        if self.spending.total_spent >= 100:
            score += 25
        elif self.spending.total_spent >= 50:
            score += 15
        elif self.spending.total_spent >= 20:
            score += 10
        # Engagement signals
        if self.qualifying.emotional_openness >= 7:
            score += 10
        if self.qualifying.message_length == "paragraph":
            score += 5
        # Subscribe reason
        if self.qualifying.subscribe_reason and \
           any(w in self.qualifying.subscribe_reason.lower()
               for w in ["different", "special", "connection", "real"]):
            score += 10
        return min(score, 100)

    def add_message(self, role: str, content: str, metadata: Dict = None):
        """Track a message in recent history."""
        msg = {
            "role": role,  # "sub" or "bot"
            "content": content,
            "timestamp": datetime.now().isoformat(),
        }
        if metadata:
            msg["metadata"] = metadata
        self.recent_messages.append(msg)
        # Keep last 50 messages
        if len(self.recent_messages) > 50:
            self.recent_messages = self.recent_messages[-50:]
        if role == "sub":
            self.last_message_date = datetime.now()
            self.last_active_date = datetime.now()
            self.message_count += 1

    def add_callback_reference(self, detail: str):
        """Store something the sub said that we can reference later for GFE."""
        self.callback_references.append(detail)
        if len(self.callback_references) > 20:
            self.callback_references = self.callback_references[-20:]

    def record_purchase(self, amount: float, content_type: str = "ppv"):
        """Record a purchase."""
        self.spending.total_spent += amount
        self.spending.last_purchase_date = datetime.now()
        if content_type == "ppv":
            self.spending.ppv_count += 1
        elif content_type == "custom":
            self.spending.custom_count += 1
        elif content_type == "tip":
            self.spending.tip_count += 1
        if amount > self.spending.highest_single_purchase:
            self.spending.highest_single_purchase = amount
        # Recalc average
        total_purchases = self.spending.ppv_count + self.spending.custom_count
        if total_purchases > 0:
            self.spending.avg_ppv_price = self.spending.total_spent / total_purchases
        # Reset objection tracking on successful purchase
        self.tier_no_count = 0
        self.brokey_flagged = False
        # Check for whale upgrade
        if self.spending.tier == SubTier.WHALE and self.sub_type != SubType.WHALE:
            self.sub_type = SubType.WHALE

    def to_dict(self) -> Dict:
        """Serialize for storage."""
        return {
            "sub_id": self.sub_id,
            "username": self.username,
            "state": self.state.value,
            "sub_type": self.sub_type.value,
            "persona_id": self.persona_id,
            "source_ig_account": self.source_ig_account,
            "qualifying": {
                "age": self.qualifying.age,
                "location": self.qualifying.location,
                "occupation": self.qualifying.occupation,
                "relationship_status": self.qualifying.relationship_status,
                "interests": self.qualifying.interests,
            },
            "spending": {
                "total_spent": self.spending.total_spent,
                "ppv_count": self.spending.ppv_count,
                "tier": self.spending.tier.value,
            },
            "whale_score": self.whale_score,
            "gfe_active": self.gfe_active,
            "callback_references": self.callback_references,
            "message_count": self.message_count,
        }


# ─────────────────────────────────────────────
# SCRIPT - Content Arc Definitions
# ─────────────────────────────────────────────

@dataclass
class ScriptStep:
    """One step in a script arc."""
    phase: ScriptPhase
    message_templates: List[str]           # Multiple options to randomize
    ppv_price: Optional[float] = None      # If this step includes a PPV
    ppv_caption_templates: List[str] = field(default_factory=list)
    content_type: Optional[str] = None     # "tease_pic", "reveal_video", etc.
    wait_for_response: bool = True         # Whether to wait for sub reply
    min_delay_seconds: int = 30            # Minimum time before sending
    conditions: Dict[str, Any] = field(default_factory=dict)  # Conditional logic


@dataclass
class Script:
    """
    A complete script arc (from Doc 7).
    Pre-designed content + caption sequence built around a theme.
    """
    script_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    name: str = ""
    theme: str = ""                        # "gym_sweaty", "wine_night", "lonely_bed"
    description: str = ""
    persona_id: str = ""                   # Which persona this belongs to
    niche: NicheType = NicheType.GIRL_NEXT_DOOR

    steps: List[ScriptStep] = field(default_factory=list)

    # Pricing for this script's value ladder
    step_prices: List[float] = field(default_factory=lambda: [10, 20, 35, 55])

    # Tags for matching to subscriber profiles
    best_for_sub_types: List[SubType] = field(default_factory=lambda: [
        SubType.HORNY, SubType.ATTRACTED
    ])
    intensity_level: int = 5               # 1-10
    requires_gfe: bool = False

    @property
    def total_potential_revenue(self) -> float:
        return sum(s.ppv_price for s in self.steps if s.ppv_price)

    @property
    def step_count(self) -> int:
        return len(self.steps)


# ─────────────────────────────────────────────
# ENGINE ACTIONS - What the bot can do
# ─────────────────────────────────────────────

@dataclass
class BotAction:
    """Represents an action the bot should take."""
    action_type: str          # "send_message", "send_ppv", "send_free", "wait", "flag"
    message: str = ""
    ppv_price: Optional[float] = None
    ppv_caption: str = ""
    content_id: Optional[str] = None     # Reference to actual content file
    delay_seconds: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)
    # State transition
    new_state: Optional[SubState] = None
    new_script_phase: Optional[ScriptPhase] = None

    def __repr__(self):
        if self.action_type == "send_ppv":
            return f"[PPV ${self.ppv_price}] {self.ppv_caption}"
        return f"[{self.action_type}] {self.message[:80]}..."
