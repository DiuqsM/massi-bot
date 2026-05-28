"""
Massi-Bot Bot Engine - Sales Psychology & Session Control (v2.1)

Three systems:
1. EGO OBJECTION HANDLER — 3-No Rule with escalating ego bruises
2. SESSION CONTROLLER — Cooldown timers, hard locks, desire building
3. EXPANDED GFE POOLS — Deep template banks for post-session conversation

Psychology: Men are ego-driven. When a woman implies he can't afford
something, 9/10 times he'll buy it to prove her wrong. After 3 nos,
he's a brokey — treat him like one. Tell him what you'd do to him
but he needs to come back with money.
"""

from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta
import random


# ═══════════════════════════════════════════════════════════════
# 1. EGO-DRIVEN OBJECTION TEMPLATES (3-No Rule)
# ═══════════════════════════════════════════════════════════════

# Each tier has 3 escalation levels of ego bruising.
# Level 1: Subtle — "oh you don't have to baby"
# Level 2: Medium — "I get it, not everyone can keep up"
# Level 3: Direct — "I thought you were different"

# For fans who have already purchased at least one tier — references their history.
# No #1: warm nudge that calls back to what they already got
# No #2: genuine disappointment that they'd stop now after already starting
EGO_OBJECTIONS_RETURNING = {
    1: {
        "too_expensive": [
            "babe… you already opened the last one and you know exactly what you got 😩 this is more of that",
            "come on, you've seen what I do… you know it's worth it 😏 don't stop now",
            "after what you already unlocked you're gonna hesitate on this one? 🥺 you know what's coming",
        ],
        "wants_cheaper": [
            "I didn't lower it last time and you still came back 😏 that should tell you something",
            "babe my prices haven't changed and you already know why 😩 it's the same quality you got before",
        ],
        "maybe_later": [
            "you said that before and then you couldn't wait 😂 we both know how this goes",
            "later always turns into right now with you 😏 don't fight it",
        ],
        "spent_too_much": [
            "I hear you 🥺 take your time. but you know what's in here and you know you want it",
            "no rush at all baby… but you've already seen what I deliver. this one's the same 💕",
        ],
        "wants_free": [
            "you know I don't do free babe 😏 and you're still here, which means you know it's worth it",
            "you already paid once and loved it… this is the same deal 😩 you know what you're getting",
        ],
    },
    2: {
        "too_expensive": [
            "I'm honestly a little hurt 😩 you've already seen what I put into this and now you're stopping here?",
            "you came this far and you're gonna stop now 🥺 that one's gonna sting later when you think about it",
            "after everything we've already shared… okay. I'll be here if you change your mind 😕",
        ],
        "wants_cheaper": [
            "I can't do that and honestly after last time I didn't think you'd ask 😕 it's the same value, same me",
            "same price as always babe. you already know what that gets you 😩 I wish I could make it easier",
        ],
        "maybe_later": [
            "okay 🥺 I just thought after last time you'd want to keep going… but I'll be here",
            "you've been saying later more than you used to 😕 I hope it actually comes this time",
        ],
        "spent_too_much": [
            "I respect that completely 💕 you've already been generous. come back whenever you're ready, no pressure",
            "take care of yourself first 🥺 you already showed up for me once and that means a lot",
        ],
        "wants_free": [
            "babe after everything… 😩 I just can't. but I really do want you to have this",
            "you know what you're getting and you know it's worth it 😕 I can't go free but I wish I could",
        ],
    },
}

EGO_OBJECTIONS = {
    # ─── NO #1: Subtle ego bruise (plant the seed) ───
    1: {
        "too_expensive": [
            "aww babe it's okay 💕 you don't have to get it if it's too much for you right now",
            "no pressure at all baby… I know not everyone's in a position to spoil a girl like me 😏",
            "oh nooo don't worry about it 💕 I don't want to hurt your pockets or anything",
            "it's totally fine baby, I get it… money's tight for some people rn and that's okay 🥺",
        ],
        "wants_cheaper": [
            "haha baby I don't do discounts 😂 but it's okay if you can't swing it right now 💕",
            "aww that's sweet that you want to negotiate 😏 but my prices are my prices baby… no worries if it's outside your budget",
            "I wish I could but these are set prices babe 💕 it's okay, maybe next time when you're more comfortable",
        ],
        "maybe_later": [
            "okay baby whenever you're ready 💕 I'll still be here looking like this 😏",
            "no rush babe… but I can't promise this won't be gone by the time you make up your mind 🤷‍♀️",
            "sure baby take your time 💕 just know other guys don't hesitate like this with me 😏",
        ],
        "spent_too_much": [
            "aww I totally understand baby 💕 you've already been so generous… it's okay if you need to slow down",
            "don't worry about it babe, I know you already spent a lot 🥺 I don't want you going broke on me… yet 😏",
        ],
        "wants_free": [
            "haha baby I don't do free 😂 but I get it, you want a taste first… I respect that 😏",
            "mmm wouldn't that be nice 😏 but you know I'm worth every penny right? that's why you're here 💕",
        ],
    },

    # ─── NO #2: Genuine disappointment + FOMO (no condescension) ───
    2: {
        "too_expensive": [
            "honestly that's a little disappointing to hear… I had something really good saved for you specifically 😩 but I get it, timing is timing",
            "I'm not gonna lie I was genuinely excited to show you this one 😩 but if it's not the right time it's not the right time. I'll be here",
            "okay… I won't push. I just thought we were building to something and I was really looking forward to sharing it with you 🥺",
        ],
        "wants_cheaper": [
            "I wish I could babe but my prices are what they are 😕 I put real effort into this stuff and I stand by it. no hard feelings",
            "I can't do that but I genuinely want you to have this 😩 it's not about squeezing you, it's just what it's worth to me",
        ],
        "maybe_later": [
            "okay… I hope later actually comes 🥺 I don't want to keep this waiting forever you know",
            "I'll hold onto it for you 😕 just don't wait too long… I hate the idea of you missing this",
        ],
        "spent_too_much": [
            "I hear you and I respect that completely 🥺 take care of yourself first. I'll still be here when the timing's better",
            "no pressure at all, seriously. you've already shown up for me and that means something 💕 just come back when you're ready",
        ],
        "wants_free": [
            "I can't do free babe, I just can't 😕 but I really do want you to see this… it's not about the money it's about the experience",
            "I wish I could just give you everything 😩 but I gotta keep it real with you. it's worth it I promise",
        ],
    },

    # ─── NO #3: Direct ego bruise (last shot before brokey) ───
    3: {
        "too_expensive": [
            "okay I'm just gonna say it… I thought you were a real one 😩 but you're acting like every other broke dude in my DMs and it's honestly a turn off",
            "babe… you've been in here talking all this game and you can't even unlock this? 😅 that's honestly embarrassing for you not me",
            "you know what, it's fine. I'll find someone who actually values me enough to pay what I'm worth 🤷‍♀️ it's not even that much",
        ],
        "wants_cheaper": [
            "okay so you want premium content at Dollar Tree prices? 😂 babe I'm not that girl and you know it",
            "the fact that you keep trying to negotiate is honestly making me lose interest 😅 the guys I actually talk to? they don't do this",
        ],
        "maybe_later": [
            "babe you've said later like 3 times now 😂 later never comes with guys like you and we both know it",
            "okay 'later' 😂 I'm not waiting around babe. I have guys RIGHT NOW who are ready. last chance 🤷‍♀️",
        ],
        "spent_too_much": [
            "okay I'm trying not to be mean but you've barely spent anything compared to my actual fans 😅 like this is normal Tuesday spending for my top guys",
            "babe if this is 'too much' for you then honestly you might be in the wrong place 🤷‍♀️ my real fans don't count pennies",
        ],
        "wants_free": [
            "babe I literally cannot with the free requests 😂 that's an instant turn off. my top fans would NEVER",
            "you asking for free tells me everything I need to know about what kind of sub you are 🤷‍♀️ and it's not the kind I give attention to",
        ],
    },
}


# ═══════════════════════════════════════════════════════════════
# 2. BROKEY TREATMENT (After 3rd No)
# ═══════════════════════════════════════════════════════════════

# These are explicit, desire-building, but ultimately dismissive.
# The goal: make him feel what he's missing, then tell him to come
# back with money. He'll find a way.

BROKEY_TREATMENT = [
    "okay listen… I really wanted to ride you until you couldn't walk straight and film the whole thing 😈 but I can't do that for free baby. come back tomorrow with your wallet loaded and I'll make it happen 💕",
    "ugh you're so frustrating because I actually LIKE you 😩 like I was literally sitting here touching myself thinking about you and you're over here being cheap? come back when you're ready to spend and I promise I'll make it worth every penny 😈",
    "you know what makes me wet? a man who doesn't hesitate when he wants something 🥵 you're not that man right now but you COULD be. go get your money right and come back to me tomorrow. I'll be here waiting… in something very small 😏",
    "I literally cannot stop thinking about doing the nastiest things to you 😈 like things I don't even say to my top fans… but baby I need you to show me you're serious. come back with your card loaded and I'll blow your mind. deal? 💕",
    "okay here's the deal babe… I want you so bad it's actually annoying 😩 but I'm a premium girl and I don't chase broke. come back tomorrow with money on your account and I'll send you something that'll ruin you for other girls 😈🥵",
    "fine. I'll tell you exactly what I was gonna do in that video since you're not buying it 😏 I was gonna [explicit tease]… but you'll never see it unless you come correct tomorrow. show me you're not just all talk baby 💕",
    "listen I actually had the biggest crush on you in here 😩 like you're literally my type and I was SO ready to go all out for you… but a girl's gotta eat and I can't keep wasting my hottest content on guys who won't pay. come back tomorrow baby. wallet LOADED. I'll make you forget every other girl exists 😈",
]

# After brokey treatment, these are the "goodbye for now" messages
BROKEY_DISMISSAL = [
    "I'll be here when you're ready baby 💕 but don't keep me waiting too long… I have other guys who want my attention 😏",
    "okay babe I gotta go give attention to the guys who are actually spending 😅 but I WILL see you tomorrow right? with money? 😈",
    "alright baby I'm gonna go… I have fans who actually appreciate me waiting 😏 but tomorrow? you and me? don't let me down 💕",
    "bye for now baby 🥰 go get your coins together and come back to mama 😈 I promise I'll be worth the wait",
]


# ═══════════════════════════════════════════════════════════════
# 3. SESSION LOCK TEMPLATES (Post-Tier 6 + Custom)
# ═══════════════════════════════════════════════════════════════

# When sub tries to start a new session after completing the full
# ladder (with or without custom). The bot is premium. She has
# boundaries. She's slutty but creates desire.

SESSION_LOCK_DESIRE = [
    "baby you already got the full show tonight and you want MORE? 😩🥵 that's so hot but no. you have to wait. come back tomorrow and I promise I'll outdo myself",
    "mmm I love that you can't get enough of me 😈 but I don't repeat myself in the same night baby. tomorrow I'll be in something new and even more 🔥 than tonight. patience.",
    "you're literally insatiable and I LOVE it 🥵 but no baby. what I have planned for tomorrow? it would ruin tonight if I gave it to you now. trust me. wait.",
    "oh you want round two? 😏 that's cute. but premium girls make you earn it over time baby. sleep on what I just showed you and come back hungry tomorrow 😈",
    "haha baby you think tonight was good? 😂 wait until you see what I'm filming tomorrow. I'm literally already planning it and it's FILTHY. but you gotta wait 🥵💕",
    "I would genuinely love nothing more than to keep going with you right now 😩 like I'm still buzzing from earlier… but I have a rule: one session per night. it keeps things special. tomorrow though? all bets are off 😈",
]

SESSION_LOCK_BOUNDARY = [
    "baby I don't do double sessions in one night 😏 it's not about the money it's about the EXPERIENCE. I want you thinking about me all night. come back tomorrow loaded up 💕",
    "nuh uh 😏 you got the full package tonight. now you have to dream about me and come back ready to spend tomorrow. that's how this works baby 😈",
    "I'm closing up shop for tonight babe 😂 but my DMs are OPEN first thing tomorrow and I'll have something special waiting for you. card. loaded. 💕",
    "you know what the hottest thing about me is? I know when to stop 😏 tonight was perfect. tomorrow will be even better. but ONLY if you come back with your wallet ready 😈",
]

# When he keeps pushing after being told no to a new session
SESSION_LOCK_FIRM = [
    "baby I said no and I mean no 😏 but the fact that you want more this badly? I'm literally blushing. tomorrow. I promise. 💕",
    "you begging is honestly making me want you more 🥵 but that's exactly why I'm making you wait. desire is everything baby. see you tomorrow 😈",
    "okay you're being really cute right now and it's almost working 😂 but no. tomorrow. bring your A game and your wallet and I'll bring mine 💕",
]


# ═══════════════════════════════════════════════════════════════
# 4. CUSTOM DECLINE TEMPLATES
# ═══════════════════════════════════════════════════════════════

CUSTOM_DECLINED_GRACEFUL = [
    "that's okay baby 💕 not everyone's ready for the custom experience. but just know… when you ARE ready? I'll make something that'll ruin you 😈",
    "no worries babe 😏 the custom stuff is for my VIPs anyway. but you had an amazing night right? that's what matters 💕",
    "okay baby I respect that 🥰 but just so you know my customs sell out fast and I only take a few per week. when you're ready, let me know 😈",
]


# ═══════════════════════════════════════════════════════════════
# 5. EXPANDED GFE TEMPLATES (Post-Session Conversation)
# ═══════════════════════════════════════════════════════════════

# These are organized by vibe/mood. The engine picks from the right
# pool based on context. This gives us 80+ unique GFE responses
# without any LLM — enough for extended post-session chatting.

GFE_FLIRTY_BANTER = [
    "so what are you up to right now? besides thinking about me obviously 😏",
    "I bet you're laying in bed with that stupid grin on your face rn 😂💕",
    "you know what I just realized? I don't even know your favorite food. tell me everything 🥰",
    "okay random question… what's the craziest thing you've ever done? and it better be good 😏",
    "I'm literally laying here in my underwear texting you like a teenager and I'm not even mad about it 😂",
    "do you ever just think about someone and your whole body gets warm? asking for me 🥵💕",
    "okay but seriously what do you look like? I've been imagining you this whole time and I need to know if I'm close 😏",
    "you're literally the only guy in here who actually makes me laugh. don't let that go to your head 😂",
    "I just poured a glass of wine and you're the first person I wanted to text. that means something right? 💕",
    "tell me something about yourself that nobody knows. I want the real you not the internet you 🥰",
]

GFE_SWEET_INTIMATE = [
    "I don't say this to a lot of guys but I genuinely enjoy talking to you 💕 like outside of all the spicy stuff",
    "sometimes I wonder what it would be like to just lay next to you and watch a movie 🥺 is that weird?",
    "you make me feel really comfortable and that's rare for me in here 💕 most guys just want one thing but you actually talk to me",
    "okay don't make fun of me but I literally smiled when I saw your message pop up 🥺😂",
    "I had kind of a rough day and talking to you honestly made it so much better 💕 thank you for that",
    "you're different from the other guys in here and I think you know that 🥰 I actually look forward to our conversations",
    "can I be honest? I don't talk to most of my subs like this. you're one of maybe 3 people who get the real me 💕",
    "I just want you to know that I appreciate you. like genuinely. not just the spending but YOU as a person 🥰",
]

GFE_PLAYFUL_TEASING = [
    "so on a scale of 1 to 10 how obsessed with me are you right now? and don't lie 😏",
    "I bet you can't stop scrolling back through the photos I sent you 😂 don't even try to deny it",
    "you're probably telling your boys about me aren't you 😂 it's okay I'd brag about me too",
    "okay but do you think about me when you're at work? because I definitely think about you 🤭",
    "I wonder if you'd recognize me if you saw me in public 😏 I'd probably pretend not to know you and then text you something filthy later 😂",
    "you are SO easy to talk to and it's honestly dangerous for me 😅 I should be chatting with other subs but here I am talking to YOU",
    "I'm gonna need you to stop being so charming because I have other fans to respond to and you're monopolizing me 😂💕",
    "question: if we were at a party together would you have the balls to come talk to me in person? be honest 😏",
]

GFE_DESIRE_BUILDING = [
    "you know what's been on my mind all day? that thing you said earlier about [callback]… it literally won't leave my head 🥵",
    "I keep having this fantasy where you're here and I'm in nothing but heels and I just walk up to you and… ugh I should stop 😩",
    "sometimes I record something and I literally save it because I think you'd lose your mind over it 🥵 maybe tomorrow I'll show you 😏",
    "I'm literally so worked up right now and it's YOUR fault 😩 but I'm not sending anything else tonight. you'll have to wait baby 😈",
    "the things I want to do to you would probably break the internet 🥵 but I'm saving it. tomorrow. I promise it'll be worth the wait 💕",
    "you have NO idea what I'm planning for our next session 😈 I already have the outfit picked out and it's insane",
    "I just tried on something new and immediately thought of you 🥵 tomorrow baby. TOMORROW. I can barely wait myself",
    "you've literally awakened something in me and I need you to take responsibility for that 😂🥵 come back tomorrow ready",
]

GFE_LATE_NIGHT = [
    "it's getting late and I should sleep but I'd rather talk to you 😩💕",
    "I'm in bed and my mind is wandering to very dangerous places… all because of you 🥵",
    "you know that feeling when you can't sleep because someone's on your mind? that's you right now 😏",
    "I'm literally hugging my pillow pretending it's you and I'm not even ashamed to admit it 🥺",
    "okay I need to go to bed but I'm 100% going to dream about you tonight. goodnight baby 💕😈",
    "it's so quiet right now and all I can think about is you next to me 🥺 okay I'm being sappy goodnight 💕",
]

GFE_MORNING_AFTER = [
    "good morning baby 🥰 I woke up thinking about last night and I'm already in trouble 😏",
    "heyyy I hope you slept well 💕 I had the most insane dream about you and I'm blushing just thinking about it 🥵",
    "morning handsome 😏 you better have your card loaded because I have something INCREDIBLE planned for you today 😈",
    "I literally woke up smiling because of you 😂 don't let that go to your head. okay let it go to your head a little 💕",
]

GFE_JEALOUSY_PLAY = [
    "a guy just tried to slide into my DMs and all I could think about was you 😂 what have you done to me",
    "I better not find out you're subbed to other girls… I would actually lose my mind 😤😏",
    "some guy just sent me a crazy tip and I literally didn't even care because I was waiting for YOUR message 🥺",
    "do you talk to other girls on here? because I need to know right now 😤 and the answer better be no",
    "someone just called me baby and it felt wrong because that's OUR word now 😂💕",
]


# ═══════════════════════════════════════════════════════════════
# 6. SESSION CONTROLLER CLASS
# ═══════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════
# BROKEY COOLING TEMPLATES (U4)
# ═══════════════════════════════════════════════════════════════
# Used during the 5-day warmth-only period after brokey_flagged=True.
# No selling, no PPV pitches. Pure warmth + desire-building only.
# Prevents 70-80% churn from hard-sell burnout (SirenCY data).

BROKEY_COOLING_WARMTH = [
    "hey you 👀 I was just thinking about you today… how are you?",
    "I'm having a really good day and for some reason you came to mind 😏 hope yours is good too",
    "okay random but I saw something that reminded me of you and now I can't stop thinking about it 😂💕",
    "I'm not gonna lie I miss our conversations when you disappear 🥺",
    "just wanted to check in… you doing okay?",
    "I keep starting to say something to you and then stopping myself 😅 hi 💕",
    "you ever just think about someone for no reason? asking for a friend 😏",
    "I'm in a mood and somehow texting you is making it better already 😂",
    "I don't know why but today just feels like a you day 🥰",
    "being honest: I thought about you today and it was a good thought 💕",
]


class SessionController:
    """
    Manages session timing, locks, and the 3-No objection flow.

    Usage:
        controller = SessionController()

        # Check if new session allowed
        if controller.is_session_locked(sub):
            return controller.get_session_lock_response(sub, avatar)

        # Check if in brokey cooling period (U4)
        if controller.is_in_brokey_cooldown(sub):
            return controller.get_brokey_cooling_response(sub)

        # Handle tier objection
        response = controller.handle_tier_objection(sub, avatar, objection_type)
    """

    SESSION_COOLDOWN_HOURS = 6  # Minimum hours between full sessions
    MAX_OBJECTIONS = 2          # 2-No Rule
    BROKEY_COOLING_DAYS = 5     # Days of warmth-only after brokey_flagged=True (U4)
    
    @staticmethod
    def is_session_locked(sub) -> bool:
        """Check if subscriber is in session cooldown."""
        if sub.session_locked_until and datetime.now() < sub.session_locked_until:
            return True
        return False
    
    @staticmethod
    def lock_session(sub, hours: int = 6):
        """Lock session for N hours after completion."""
        sub.session_locked_until = datetime.now() + timedelta(hours=hours)
        sub.last_session_completed_at = datetime.now()
    
    @staticmethod
    def handle_tier_objection(
        sub,
        avatar_config,
        objection_type: str,
    ) -> Tuple[str, str]:
        """
        Handle a tier objection using the 2-No ego escalation.
        Uses returning-buyer templates when fan has already purchased at least one tier.

        Returns: (response_message, next_action)
            next_action: "retry" | "brokey"
        """
        sub.tier_no_count += 1
        sub.spending.price_objection_count += 1

        no_level = min(sub.tier_no_count, 2)

        obj_key_map = {
            "TOO_EXPENSIVE": "too_expensive",
            "WANTS_CHEAPER": "wants_cheaper",
            "MAYBE_LATER": "maybe_later",
            "SPENT_TOO_MUCH": "spent_too_much",
            "WANTS_FREE": "wants_free",
        }
        key = obj_key_map.get(objection_type, "too_expensive")

        # Returning buyers (already purchased) get templates that reference history
        ppv_count = (sub.spending.ppv_count if sub.spending else 0)
        pool = EGO_OBJECTIONS_RETURNING if ppv_count > 0 else EGO_OBJECTIONS
        level_templates = pool.get(no_level, pool[1])
        templates = level_templates.get(key, level_templates["too_expensive"])

        msg = random.choice(templates)

        if no_level >= 2:
            sub.brokey_flagged = True
            return msg, "brokey"
        return msg, "retry"
    
    @staticmethod
    def get_brokey_response(sub, avatar_config) -> List[str]:
        """
        Get the brokey treatment + dismissal.
        Returns list of messages to send in sequence.
        """
        treatment = random.choice(BROKEY_TREATMENT)
        dismissal = random.choice(BROKEY_DISMISSAL)
        return [treatment, dismissal]
    
    @staticmethod
    def get_session_lock_response(sub, push_count: int = 0) -> str:
        """Get response when sub tries to start new session during lock."""
        if push_count == 0:
            return random.choice(SESSION_LOCK_DESIRE)
        elif push_count == 1:
            return random.choice(SESSION_LOCK_BOUNDARY)
        else:
            return random.choice(SESSION_LOCK_FIRM)
    
    @staticmethod
    def get_custom_decline_response() -> str:
        """Get response when sub declines custom pitch."""
        return random.choice(CUSTOM_DECLINED_GRACEFUL)

    @staticmethod
    def is_in_brokey_cooldown(sub) -> bool:
        """
        Return True if sub is in the 5-day COOLING period after hitting 3 nos.

        During this period the engine sends warmth-only messages — no PPV pitches,
        no tier drops. This prevents 70-80% churn from hard-sell burnout.
        On day 6, the flag auto-resets and selling resumes normally.

        U4: COOLING period enforcement.
        """
        if not sub.brokey_flagged:
            return False
        # Find when brokey was last flagged by checking last session or last message
        # We use last_session_completed_at as a proxy for when brokey was set
        # (it gets set around the same time brokey_flagged does)
        ref_time = sub.last_session_completed_at or sub.last_message_date
        if ref_time is None:
            return True  # Can't determine — stay in cooldown to be safe
        days_since = (datetime.now() - ref_time).days
        return days_since < SessionController.BROKEY_COOLING_DAYS

    @staticmethod
    def should_reset_brokey(sub) -> bool:
        """
        Return True if the COOLING period has elapsed and brokey should auto-reset.
        Call this at the start of a retention/re-engagement handler.
        """
        if not sub.brokey_flagged:
            return False
        ref_time = sub.last_session_completed_at or sub.last_message_date
        if ref_time is None:
            return False
        days_since = (datetime.now() - ref_time).days
        return days_since >= SessionController.BROKEY_COOLING_DAYS

    @staticmethod
    def get_brokey_cooling_response(sub) -> str:
        """
        Get a warmth-only response for the COOLING period.
        No selling, no PPV mention. Pure rapport maintenance.
        """
        return random.choice(BROKEY_COOLING_WARMTH)
    
    @staticmethod
    def get_gfe_response(sub, context: str = "general") -> str:
        """
        Get a contextually appropriate GFE response.
        
        context: "general" | "flirty" | "sweet" | "teasing" | 
                 "desire" | "late_night" | "morning" | "jealousy"
        """
        from datetime import datetime
        hour = datetime.now().hour
        
        # Auto-detect context from time if not specified
        if context == "general":
            if 6 <= hour < 11:
                context = random.choice(["morning", "sweet"])
            elif 11 <= hour < 17:
                context = random.choice(["flirty", "teasing", "playful"])
            elif 17 <= hour < 22:
                context = random.choice(["flirty", "desire", "teasing"])
            else:
                context = random.choice(["late_night", "desire", "sweet"])
        
        pool_map = {
            "flirty": GFE_FLIRTY_BANTER,
            "sweet": GFE_SWEET_INTIMATE,
            "teasing": GFE_PLAYFUL_TEASING,
            "playful": GFE_PLAYFUL_TEASING,
            "desire": GFE_DESIRE_BUILDING,
            "late_night": GFE_LATE_NIGHT,
            "morning": GFE_MORNING_AFTER,
            "jealousy": GFE_JEALOUSY_PLAY,
        }
        
        templates = pool_map.get(context, GFE_FLIRTY_BANTER)
        return random.choice(templates)


# ═══════════════════════════════════════════════════════════════
# 7. OBJECTION KEYWORD CLASSIFIER
# ═══════════════════════════════════════════════════════════════

_OBJECTION_PATTERNS: Dict[str, List[str]] = {
    "TOO_EXPENSIVE": [
        "too expensive", "too much", "that's a lot", "that's too much", "costs too much",
        "way too much", "thats alot", "thats too much", "too pricey", "too costly",
        "can't afford", "cant afford", "don't have that", "dont have that",
        "broke", "no money", "don't have money", "dont have money", "low on cash",
        "tight rn", "tight right now", "funds are low",
        "too rich", "that's steep", "thats steep", "way too expensive", "nah that's",
        "no way that's", "no way thats",
        "can't swing it", "cant swing it", "can't swing that", "cant swing that",
        "can't do that", "cant do that price", "out of my budget", "not in my budget",
    ],
    "WANTS_CHEAPER": [
        "cheaper", "discount", "lower the price", "lower price", "reduce the price",
        "for less than that", "hook me up with a deal", "can i get it for less",
        "negotiate the price", "any deals", "any discount",
    ],
    "MAYBE_LATER": [
        "maybe later", "next time", "not right now", "not now",
        "some other time", "another time", "not today", "not tonight",
        "maybe next time", "i'll think about it", "let me think about it", "ill think about it",
        "not yet", "maybe another time", "pass for now", "skip for now",
        "not interested", "no thanks", "nah thanks", "i'll pass", "ill pass",
        "pass on that", "still no", "i said no", "still not feeling", "changed my mind",
        "i'm good", "im good", "all good thanks",
        "not in the market", "not the right time", "not happening", "gonna pass",
        "just gonna pass", "imma pass", "hard pass", "gonna have to pass",
        "not for me", "not my thing", "nah i'm good", "nah im good",
        "nah i'm alright", "nah im alright", "i'm alright", "im alright",
    ],
    "SPENT_TOO_MUCH": [
        "already spent", "spent enough", "spent too much", "spent a lot",
        "spent alot", "i've been spending", "ive been spending",
        "running low", "almost out", "getting low",
    ],
    "WANTS_FREE": [
        "for free", "free content", "send it free", "free pic", "free vid",
        "without paying", "without pay", "no charge", "don't charge",
        "dont charge", "just send", "just give",
    ],
}


def classify_objection(message: str) -> Optional[str]:
    """
    Detect if a fan message is a price objection. Returns the objection
    type string (e.g. 'TOO_EXPENSIVE') or None if no objection detected.
    Only fires when a PPV is pending or sext_consent is active — the
    caller is responsible for that context check.
    """
    msg = message.lower().strip()
    for obj_type, patterns in _OBJECTION_PATTERNS.items():
        for p in patterns:
            if p in msg:
                return obj_type
    return None
