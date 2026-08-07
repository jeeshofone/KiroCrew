"""Tests for SessionMap channel-neutral outbound mirror binding.

Covers the C1 generalization of the Slack-only dashboard->channel mirror into a
channel-agnostic ``ChannelLink`` binding: non-Slack targets are stored under
``mirror``; Slack routes back through the dedicated slack-link fields (keeping
its reverse index intact); legacy Slack sessions surface as a synthesized
Slack ``ChannelLink`` without needing migration.
"""

from __future__ import annotations

import json
import os
from unittest.mock import patch

import pytest

from kiro_crew.messaging.link import (
    ChannelLink,
    legacy_dashboard_mirror_key,
    release_conversation_location,
)
from kiro_crew.session_map import InboundOwnershipConflict, SessionMap


@pytest.fixture()
def session_map(tmp_path):
    """A SessionMap backed by a temp directory."""
    with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
        yield SessionMap()


class TestNonSlackMirror:
    def test_set_get_round_trip(self, session_map):
        link = ChannelLink(channel_type="telegram", channel_id="12345", thread_id=None)
        session_map.set_mirror_link("dashboard:chat-1", link)
        got = session_map.get_mirror_link("dashboard:chat-1")
        assert got == link

    def test_stored_under_mirrors_keyed_by_channel_type(self, session_map):
        """One binding per channel type, so the map is keyed by it.

        This asserts the on-disk SHAPE, which changed when a session became able
        to hold several bindings. The legacy single-``mirror`` shape is still
        read (see TestLegacySingleBindingCompat) — it is simply no longer written.
        """
        session_map.set_mirror_link(
            "dashboard:chat-1", ChannelLink(channel_type="telegram", channel_id="99")
        )
        entry = session_map._data["dashboard:chat-1"]
        assert entry["mirrors"]["telegram"]["channel_id"] == "99"
        assert "mirror" not in entry

    def test_does_not_touch_slack_link(self, session_map):
        session_map.set_mirror_link(
            "dashboard:chat-1", ChannelLink(channel_type="telegram", channel_id="99")
        )
        # A telegram mirror is NOT a Slack link.
        assert session_map.get_slack_link("dashboard:chat-1") == (None, None)

    def test_creates_entry_when_absent(self, session_map):
        session_map.set_mirror_link(
            "fresh:key", ChannelLink(channel_type="telegram", channel_id="1")
        )
        assert "fresh:key" in session_map._data

    def test_overwrites_existing_mirror(self, session_map):
        session_map.set_mirror_link(
            "dashboard:chat-1", ChannelLink(channel_type="telegram", channel_id="1")
        )
        session_map.set_mirror_link(
            "dashboard:chat-1", ChannelLink(channel_type="telegram", channel_id="2")
        )
        got = session_map.get_mirror_link("dashboard:chat-1")
        assert got is not None and got.channel_id == "2"


class TestSlackRouting:
    def test_set_mirror_routes_to_slack_link(self, session_map):
        session_map.set("dashboard:chat-1", "sid-abc")
        session_map.set_mirror_link(
            "dashboard:chat-1",
            ChannelLink(channel_type="slack", channel_id="C1", thread_id="ts-1"),
        )
        # Routed through the dedicated Slack fields + reverse index.
        assert session_map.get_slack_link("dashboard:chat-1") == ("ts-1", "C1")
        assert session_map.get_session_for_thread("ts-1") == "dashboard:chat-1"
        # No parallel ``mirror`` field is written for Slack.
        assert "mirror" not in session_map._data["dashboard:chat-1"]

    def test_get_mirror_reflects_slack_link(self, session_map):
        session_map.set("dashboard:chat-1", "sid-abc")
        session_map.set_mirror_link(
            "dashboard:chat-1",
            ChannelLink(channel_type="slack", channel_id="C1", thread_id="ts-1"),
        )
        got = session_map.get_mirror_link("dashboard:chat-1")
        assert got == ChannelLink(channel_type="slack", channel_id="C1", thread_id="ts-1")


class TestLegacyFallback:
    def test_slack_link_surfaces_as_mirror(self, session_map):
        # A session linked via the legacy slack path (no explicit ``mirror``).
        session_map.set("dashboard:chat-1", "sid-abc")
        session_map.set_slack_link("dashboard:chat-1", "ts-9", "C9")
        assert "mirror" not in session_map._data["dashboard:chat-1"]
        got = session_map.get_mirror_link("dashboard:chat-1")
        assert got == ChannelLink(channel_type="slack", channel_id="C9", thread_id="ts-9")

    def test_channel_only_legacy_link(self, session_map):
        session_map.set("dashboard:chat-1", "sid-abc")
        session_map._data["dashboard:chat-1"]["slack_channel_id"] = "C9"
        session_map._data["dashboard:chat-1"]["slack_thread_ts"] = None
        got = session_map.get_mirror_link("dashboard:chat-1")
        assert got == ChannelLink(channel_type="slack", channel_id="C9", thread_id=None)


class TestGetMirrorLinkNone:
    def test_no_entry(self, session_map):
        assert session_map.get_mirror_link("nope:key") is None

    def test_entry_without_link(self, session_map):
        session_map.set("dashboard:chat-1", "sid-abc")
        assert session_map.get_mirror_link("dashboard:chat-1") is None


class TestMirrorReverseLookup:
    def test_outbound_only_mirror_is_not_an_inbound_route(self, session_map):
        link = ChannelLink(channel_type="discord", channel_id="dm-1")
        session_map.set_mirror_link("dashboard:chat-1", link)

        assert session_map.find_mirror_sessions(link) == ["dashboard:chat-1"]
        assert session_map.find_mirror_sessions(link, inbound_only=True) == []

    def test_resume_binding_is_found_by_exact_location(self, session_map):
        link = ChannelLink(channel_type="discord", channel_id="dm-1")
        session_map.set_mirror_link(
            "dashboard:chat-1",
            link,
            accepts_inbound=True,
        )

        assert session_map.find_mirror_sessions(link, inbound_only=True) == [
            "dashboard:chat-1"
        ]
        assert session_map.find_mirror_sessions(
            ChannelLink(channel_type="discord", channel_id="dm-2"),
            inbound_only=True,
        ) == []

    def test_duplicate_locations_are_explicit_not_arbitrarily_resolved(self, session_map):
        # Written straight into the map, because `set_mirror_link` now REFUSES to
        # create this state (see TestInboundOwnershipIsExclusive below). The reader
        # contract still has to hold for it: a map file written before that check
        # existed can carry two inbound owners, and the reader must report BOTH so
        # the resolver refuses to pick and conflict detection can see it — silently
        # resolving to one is how a reply reaches the wrong session.
        link = ChannelLink(channel_type="discord", channel_id="dm-1")
        for key in ("dashboard:chat-1", "dashboard:chat-2"):
            session_map._data[key] = {
                "mirrors": {"discord": {**link.to_dict(), "accepts_inbound": True}}
            }

        assert session_map.find_mirror_sessions(link, inbound_only=True) == [
            "dashboard:chat-1",
            "dashboard:chat-2",
        ]

    def test_outbound_overwrite_removes_inbound_marker(self, session_map):
        link = ChannelLink(channel_type="discord", channel_id="dm-1")
        session_map.set_mirror_link("dashboard:chat-1", link, accepts_inbound=True)
        session_map.set_mirror_link("dashboard:chat-1", link)

        assert session_map.find_mirror_sessions(link, inbound_only=True) == []
        assert "mirror_accepts_inbound" not in session_map._data["dashboard:chat-1"]


class TestClearMirrorLink:
    def test_clear_non_slack(self, session_map):
        session_map.set_mirror_link(
            "dashboard:chat-1", ChannelLink(channel_type="telegram", channel_id="1")
        )
        assert session_map.clear_mirror_link("dashboard:chat-1") is True
        assert session_map.get_mirror_link("dashboard:chat-1") is None

    def test_clear_slack_routes_and_evicts_reverse_index(self, session_map):
        session_map.set("dashboard:chat-1", "sid-abc")
        session_map.set_mirror_link(
            "dashboard:chat-1",
            ChannelLink(channel_type="slack", channel_id="C1", thread_id="ts-1"),
        )
        assert session_map.get_session_for_thread("ts-1") == "dashboard:chat-1"
        assert session_map.clear_mirror_link("dashboard:chat-1") is True
        assert session_map.get_mirror_link("dashboard:chat-1") is None
        assert session_map.get_session_for_thread("ts-1") is None

    def test_clear_returns_false_when_absent(self, session_map):
        session_map.set("dashboard:chat-1", "sid-abc")
        assert session_map.clear_mirror_link("dashboard:chat-1") is False

    def test_clear_returns_false_when_no_entry(self, session_map):
        assert session_map.clear_mirror_link("nope:key") is False

    def test_set_none_clears(self, session_map):
        session_map.set_mirror_link(
            "dashboard:chat-1", ChannelLink(channel_type="telegram", channel_id="1")
        )
        session_map.set_mirror_link("dashboard:chat-1", None)
        assert session_map.get_mirror_link("dashboard:chat-1") is None


class TestClearMirrorLinksAt:
    LINK = ChannelLink(channel_type="discord", channel_id="chan-1")

    def test_clears_every_spelling_at_the_location(self, session_map):
        # The stale-mirror shape: rows under key spellings the conversation no
        # longer derives (rotated generation, pre-unification dashboard row)
        # plus a dashboard session mirroring in — all at one location.
        session_map.set_mirror_link("discord:agent:direct:u1", self.LINK)
        session_map.set_mirror_link("dashboard:discord_agent_direct_u1", self.LINK)
        session_map.set_mirror_link("dashboard:chat-3", self.LINK)
        cleared = session_map.clear_mirror_links_at(self.LINK)
        assert sorted(cleared) == [
            "dashboard:chat-3",
            "dashboard:discord_agent_direct_u1",
            "discord:agent:direct:u1",
        ]
        assert session_map.find_mirror_sessions(self.LINK) == []

    def test_returns_empty_when_location_free(self, session_map):
        session_map.set_mirror_link("dashboard:chat-1", self.LINK)
        other = ChannelLink(channel_type="discord", channel_id="chan-2")
        assert session_map.clear_mirror_links_at(other) == []
        assert session_map.get_mirror_link("dashboard:chat-1") == self.LINK

    def test_no_save_when_location_free(self, session_map):
        # An empty sweep must not touch disk — the common case is `!unlink`
        # on an unlinked conversation.
        with patch.object(session_map, "_save") as save:
            assert session_map.clear_mirror_links_at(self.LINK) == []
        save.assert_not_called()

    def test_exact_location_match_includes_thread(self, session_map):
        topic = ChannelLink(channel_type="telegram", channel_id="7", thread_id="42")
        general = ChannelLink(channel_type="telegram", channel_id="7", thread_id=None)
        session_map.set_mirror_link("dashboard:chat-1", topic)
        assert session_map.clear_mirror_links_at(general) == []
        assert session_map.clear_mirror_links_at(topic) == ["dashboard:chat-1"]

    def test_clears_inbound_resume_binding_and_marker(self, session_map):
        # Duplicate/corrupt inbound bindings are exactly what the inbound
        # resolver refuses to pick from — the location sweep is the repair.
        session_map.set_mirror_link("dashboard:chat-1", self.LINK, accepts_inbound=True)
        assert session_map.clear_mirror_links_at(self.LINK) == ["dashboard:chat-1"]
        assert session_map.mirror_accepts_inbound("dashboard:chat-1") is False
        assert session_map.get_mirror_link("dashboard:chat-1") is None

    def test_slack_bindings_are_out_of_scope(self, session_map):
        session_map.set(
            "dashboard:chat-1", "sid-abc"
        )  # Slack link needs an entry to attach to
        session_map.set_mirror_link(
            "dashboard:chat-1",
            ChannelLink(channel_type="slack", channel_id="C1", thread_id="ts-1"),
        )
        slack = ChannelLink(channel_type="slack", channel_id="C1", thread_id="ts-1")
        assert session_map.clear_mirror_links_at(slack) == []
        assert session_map.get_session_for_thread("ts-1") == "dashboard:chat-1"

    def test_cleared_rows_survive_reload(self, session_map, tmp_path):
        # The sweep must persist: a clear that only mutates memory would
        # resurrect the stale binding on the next gateway start.
        session_map.set_mirror_link("dashboard:chat-1", self.LINK)
        session_map.clear_mirror_links_at(self.LINK)
        with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
            reloaded = SessionMap()
        assert reloaded.find_mirror_sessions(self.LINK) == []


class TestReleaseConversationLocation:
    """The shared in-channel unlink, composed against the REAL SessionMap."""

    KEY = "discord:agent:direct:u1"
    LINK = ChannelLink(channel_type="discord", channel_id="chan-1")

    def test_free_location_reports_not_linked(self, session_map):
        reply, swept = release_conversation_location(
            session_map, key=self.KEY, location=self.LINK, channel="discord"
        )
        assert reply == "This conversation wasn't linked."
        assert swept == []

    def test_own_binding_reports_plain_success(self, session_map):
        session_map.set_mirror_link(self.KEY, self.LINK)
        reply, swept = release_conversation_location(
            session_map, key=self.KEY, location=self.LINK, channel="discord"
        )
        # The conversation's own row falls to the key-addressed clear BEFORE
        # the sweep runs, so one binding is never double-counted.
        assert reply == "✅ Unlinked."
        assert swept == []
        assert session_map.find_mirror_sessions(self.LINK) == []

    def test_stranded_and_foreign_rows_are_counted(self, session_map):
        # Own binding + a row stranded under a rotated-generation spelling +
        # a dashboard session mirroring in: one call frees the location and
        # the reply owns up to the full count.
        session_map.set_mirror_link(self.KEY, self.LINK)
        session_map.set_mirror_link(f"{self.KEY}:gen1", self.LINK)
        session_map.set_mirror_link("dashboard:chat-9", self.LINK)
        reply, swept = release_conversation_location(
            session_map, key=self.KEY, location=self.LINK, channel="discord"
        )
        assert reply == "✅ Unlinked (3 bindings)."
        assert sorted(swept) == ["dashboard:chat-9", f"{self.KEY}:gen1"]
        assert session_map.find_mirror_sessions(self.LINK) == []

    def test_legacy_spelling_row_counted_once(self, session_map):
        # A pre-unification row is reachable by the legacy key clear; the
        # sweep must not see it again.
        session_map.set_mirror_link(legacy_dashboard_mirror_key(self.KEY), self.LINK)
        reply, swept = release_conversation_location(
            session_map, key=self.KEY, location=self.LINK, channel="discord"
        )
        assert reply == "✅ Unlinked."
        assert swept == []


class TestPrunePreservesMirror:
    def test_mirror_only_entry_survives_prune(self, tmp_path):
        with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
            sm = SessionMap()
            # No sid yet, no Slack thread — only a non-Slack mirror binding.
            sm.set_mirror_link(
                "dashboard:chat-1", ChannelLink(channel_type="telegram", channel_id="1")
            )
            pruned = sm.prune()
            assert pruned == 0
            assert sm.get_mirror_link("dashboard:chat-1") is not None


class TestPersistence:
    def test_inbound_resume_marker_round_trips_to_disk(self, tmp_path):
        link = ChannelLink(channel_type="discord", channel_id="dm-1")
        with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
            sm = SessionMap()
            sm.set_mirror_link("dashboard:chat-1", link, accepts_inbound=True)
        with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
            sm2 = SessionMap()
            assert sm2.find_mirror_sessions(link, inbound_only=True) == [
                "dashboard:chat-1"
            ]

    def test_mirror_round_trips_to_disk(self, tmp_path):
        with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
            sm = SessionMap()
            sm.set_mirror_link(
                "dashboard:chat-1",
                ChannelLink(channel_type="telegram", channel_id="777", thread_id=None),
            )
        with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
            sm2 = SessionMap()
            got = sm2.get_mirror_link("dashboard:chat-1")
            assert got == ChannelLink(channel_type="telegram", channel_id="777", thread_id=None)


class TestLegacyDashboardSpelling:
    """A channel conversation's mirror now lives on its own session key; a
    binding written under the old ``dashboard:<safe key>`` spelling must still
    resolve and still be clearable, so an existing link is not orphaned."""

    CHANNEL = "telegram:kirocrew:direct:7"
    LEGACY = "dashboard:telegram_kirocrew_direct_7"

    def test_read_falls_back_to_legacy_row(self, session_map):
        link = ChannelLink(channel_type="telegram", channel_id="7")
        session_map.set_mirror_link(self.LEGACY, link)
        assert session_map.get_mirror_link(self.CHANNEL) == link

    def test_clear_reaches_legacy_row(self, session_map):
        session_map.set_mirror_link(
            self.LEGACY, ChannelLink(channel_type="telegram", channel_id="7")
        )
        assert session_map.clear_mirror_link(self.CHANNEL) is True
        assert session_map.get_mirror_link(self.CHANNEL) is None

    def test_canonical_binding_wins_over_legacy(self, session_map):
        session_map.set_mirror_link(
            self.LEGACY, ChannelLink(channel_type="telegram", channel_id="old")
        )
        fresh = ChannelLink(channel_type="telegram", channel_id="new")
        session_map.set_mirror_link(self.CHANNEL, fresh)
        assert session_map.get_mirror_link(self.CHANNEL) == fresh

    def test_no_fallback_for_dashboard_born_key(self, session_map):
        # Only a channel key has a legacy twin; a dashboard session must not
        # inherit a binding from some unrelated sanitized name.
        assert session_map.get_mirror_link("dashboard:chat-1") is None


class TestInboundOwnershipIsExclusive:
    """At most one key may own inbound at a conversation, enforced atomically.

    Both claimants — the dashboard connect endpoint and the Discord
    session-selection button — precheck occupancy and then write, but under
    DIFFERENT locks, so their prechecks can both pass before either writes. The
    check inside `set_mirror_link` runs while `_mutate_lock` is held, which is the
    one mutex both writers pass through, so check-and-claim is atomic there.
    """

    def test_a_second_session_cannot_claim_inbound_at_the_same_conversation(
        self, session_map
    ):
        link = ChannelLink(channel_type="discord", channel_id="dm-1")
        session_map.set_mirror_link("dashboard:chat-1", link, accepts_inbound=True)

        with pytest.raises(InboundOwnershipConflict):
            session_map.set_mirror_link("dashboard:chat-2", link, accepts_inbound=True)

        # The refusal leaves the incumbent untouched — no partial write.
        assert session_map.find_mirror_sessions(link, inbound_only=True) == [
            "dashboard:chat-1"
        ]
        assert session_map.get_mirror_link("dashboard:chat-2") is None

    def test_a_takeover_still_works_because_it_evicts_before_claiming(self, session_map):
        """The check must refuse a LOST RACE, not a legitimate takeover.

        The connect endpoint clears the location and then claims it, so by the time
        it writes no rival holds the conversation. If this test ever fails, the
        atomic check has started deleting the product requirement that a user may
        take a conversation from another session after confirming.
        """
        link = ChannelLink(channel_type="discord", channel_id="dm-1")
        session_map.set_mirror_link("dashboard:chat-1", link, accepts_inbound=True)

        session_map.clear_mirror_links_at(link)
        session_map.set_mirror_link("dashboard:chat-2", link, accepts_inbound=True)

        assert session_map.find_mirror_sessions(link, inbound_only=True) == [
            "dashboard:chat-2"
        ]

    def test_a_session_may_reclaim_its_own_conversation(self, session_map):
        """A reconnect re-asserts `accepts_inbound` on a binding it already owns."""
        link = ChannelLink(channel_type="discord", channel_id="dm-1")
        session_map.set_mirror_link("dashboard:chat-1", link, accepts_inbound=True)
        session_map.set_mirror_paused("dashboard:chat-1", True, "discord")

        # Must not raise: the only inbound owner here is this very key.
        session_map.set_mirror_link("dashboard:chat-1", link, accepts_inbound=True)

        assert session_map.is_mirror_paused("dashboard:chat-1", "discord") is not True

    def test_a_legacy_row_holding_the_binding_counts_as_the_same_session(
        self, session_map
    ):
        """The legacy `dashboard:`-spelled row is SELF, not a rival.

        A channel session's binding may still sit on the pre-unification
        `dashboard:`-spelled row; this same writer consolidates it onto the
        canonical row, so at check time it is still on the legacy spelling. Reading
        that as another owner would refuse the session's own reconnect. Uses a
        CHANNEL session key because that is the only shape where the legacy fallback
        applies (`SessionMap._mirror_key` gates it on `is_channel_session_key`).
        """
        link = ChannelLink(channel_type="discord", channel_id="dm-1")
        key = "discord:dm-1"
        session_map._data[legacy_dashboard_mirror_key(key)] = {
            "mirrors": {"discord": {**link.to_dict(), "accepts_inbound": True}}
        }

        session_map.set_mirror_link(key, link, accepts_inbound=True)

        # Consolidated onto the canonical row, and still exactly one owner.
        assert session_map.find_mirror_sessions(link, inbound_only=True) == [key]

    def test_outbound_bindings_are_not_exclusive(self, session_map):
        """Two OUTBOUND bindings may share a location; an inbound claim may not.

        The guard sits inside the `accepts_inbound` branch, so the outbound writers
        in the transport dispatchers are untouched — they never set the flag. What is
        exclusive is putting a SESSION on a conversation via an inbound claim, which
        `test_an_outbound_occupant_also_blocks_an_inbound_claim` covers.
        """
        link = ChannelLink(channel_type="discord", channel_id="dm-1")
        session_map.set_mirror_link("dashboard:chat-1", link)
        session_map.set_mirror_link("dashboard:chat-2", link)

        assert session_map.find_mirror_sessions(link) == [
            "dashboard:chat-1",
            "dashboard:chat-2",
        ]
        assert session_map.find_mirror_sessions(link, inbound_only=True) == []

    def test_an_outbound_occupant_also_blocks_an_inbound_claim(self, session_map):
        """The atomic check must agree with the endpoint precheck on "occupied".

        The precheck uses the unfiltered `find_mirror_sessions(link)`, so it treats a
        plain outbound binding as an occupant and asks for confirmation. Filtering to
        inbound here made this backstop weaker than the gate it backs: an outbound
        binding arriving in the window (a concurrent Discord `!link`) was invisible,
        and the claim landed beside it — two sessions delivering into one conversation.
        """
        link = ChannelLink(channel_type="discord", channel_id="dm-1")
        session_map.set_mirror_link("dashboard:chat-1", link)  # outbound only

        with pytest.raises(InboundOwnershipConflict):
            session_map.set_mirror_link("dashboard:chat-2", link, accepts_inbound=True)

        # Refused cleanly: the outbound occupant is untouched and no partial write.
        assert session_map.find_mirror_sessions(link) == ["dashboard:chat-1"]
        assert session_map.get_mirror_link("dashboard:chat-2") is None

    def test_a_confirmed_takeover_still_displaces_an_outbound_occupant(self, session_map):
        """Stricter occupancy must not cost the takeover: eviction clears all kinds."""
        link = ChannelLink(channel_type="discord", channel_id="dm-1")
        session_map.set_mirror_link("dashboard:chat-1", link)  # outbound only

        session_map.replace_mirror_owner("dashboard:chat-2", link, accepts_inbound=True)

        assert session_map.find_mirror_sessions(link) == ["dashboard:chat-2"]


class TestTheTakeoverLeavesNoVacancy:
    """Eviction and replacement are ONE mutation, so the location is never free.

    As two calls — clear, then claim — a confirmed takeover briefly left the
    conversation with no owner. The Discord picker could claim that vacancy, and the
    takeover was then refused by the exclusivity check while the evicted binding
    stayed deleted: the user lost their link and nobody gained one.
    """

    LINK = ChannelLink(channel_type="discord", channel_id="dm-1")

    def test_the_conversation_has_an_owner_at_every_observable_point(self, session_map):
        session_map.set_mirror_link("dashboard:chat-1", self.LINK, accepts_inbound=True)

        # Observe from inside the mutation: `_write_mirrors` runs while the lock is
        # held, so a reader here sees exactly the intermediate states a rival would.
        seen: list[list[str]] = []
        original = session_map._write_mirrors

        def _spy(entry, mirrors):
            result = original(entry, mirrors)
            seen.append(session_map.find_mirror_sessions(self.LINK))
            return result

        session_map._write_mirrors = _spy  # type: ignore[method-assign]
        try:
            displaced = session_map.replace_mirror_owner(
                "dashboard:chat-2", self.LINK, accepts_inbound=True
            )
        finally:
            session_map._write_mirrors = original  # type: ignore[method-assign]

        assert session_map.find_mirror_sessions(self.LINK) == ["dashboard:chat-2"]
        assert [("dashboard:chat-1", self.LINK, True, False)] == [
            (k, ln, inb, p) for k, ln, inb, p in displaced
        ]
        assert seen, "the spy never observed an intermediate write"

    def test_the_displaced_binding_comes_back_with_its_flags(self, session_map):
        """The snapshot must carry the mute, or a failed takeover un-mutes a channel."""
        session_map.set_mirror_link("dashboard:chat-1", self.LINK, accepts_inbound=True)
        session_map.set_mirror_paused("dashboard:chat-1", True, "discord")

        displaced = session_map.replace_mirror_owner("dashboard:chat-2", self.LINK)

        assert displaced == [("dashboard:chat-1", self.LINK, True, True)], (
            "the mute did not travel with the snapshot, so a rollback would "
            "silently reconnect a muted binding"
        )

    def test_a_refused_claim_puts_the_eviction_back(self, session_map):
        """All-or-nothing: the caller must never inherit a half-done takeover."""
        session_map.set_mirror_link("dashboard:chat-1", self.LINK, accepts_inbound=True)
        calls = {"n": 0}
        original = session_map._save

        def _fail_the_claim():
            # Save #1 is the eviction, #2 is the claim. Failing the CLAIM is the case
            # under test: the eviction has committed and the replacement did not.
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("disk full")
            return original()

        session_map._save = _fail_the_claim  # type: ignore[method-assign]
        try:
            with pytest.raises(OSError):
                session_map.replace_mirror_owner("dashboard:chat-2", self.LINK)
        finally:
            session_map._save = original  # type: ignore[method-assign]

        assert session_map.find_mirror_sessions(self.LINK) == ["dashboard:chat-1"], (
            "the evicted owner was not restored after the claim was refused"
        )

    def test_an_unreadable_occupant_is_still_evicted(self, session_map):
        """Eviction follows occupancy, not snapshot readability.

        An occupant whose binding cannot be read is still holding the location;
        skipping its eviction would leave it there and get the claim refused.
        """
        session_map.set_mirror_link("dashboard:chat-1", self.LINK, accepts_inbound=True)
        session_map.get_mirror_link = lambda *a, **k: None  # type: ignore[method-assign]

        displaced = session_map.replace_mirror_owner("dashboard:chat-2", self.LINK)

        assert displaced == []
        assert session_map.find_mirror_sessions(self.LINK) == ["dashboard:chat-2"]


class TestARivalCannotLandInsideTheTakeover:
    """The real protection: a rival WRITE cannot land between eviction and claim.

    A rival that merely READS the gap is harmless — reads take no lock, but the
    exclusivity check inside the claim catches it and it is refused. What must be
    impossible is a rival WRITE committing inside the window, because then the
    takeover is refused, its rollback restores the previous owner, and the
    conversation ends up with TWO inbound owners: the one that slipped in and the
    one that was put back.

    Needs real threads and a widened window: the window exists but is far too small
    to lose naturally, so a plain race would pass with or without the fix.
    """

    LINK = ChannelLink(channel_type="discord", channel_id="dm-1")

    def test_exactly_one_owner_survives_a_concurrent_claim(self, session_map):
        import concurrent.futures
        import time

        session_map.set_mirror_link("dashboard:chat-1", self.LINK, accepts_inbound=True)

        original_clear = session_map.clear_mirror_links_at

        def _slow_clear(link):
            # Delay AFTER the eviction returns, i.e. exactly in the evict→claim gap.
            # Placed here deliberately: a sleep inside `_write_mirrors` would run
            # while an inner mutator still holds the lock, so it would not widen the
            # window this test is about and the test would pass either way.
            result = original_clear(link)
            time.sleep(0.05)
            return result

        session_map.clear_mirror_links_at = _slow_clear  # type: ignore[method-assign]

        def _takeover():
            try:
                session_map.replace_mirror_owner("dashboard:chat-2", self.LINK)
                return None
            except InboundOwnershipConflict as exc:
                return exc

        def _rival():
            time.sleep(0.02)  # aim for the middle of the gap
            try:
                session_map.set_mirror_link(
                    "dashboard:chat-3", self.LINK, accepts_inbound=True
                )
            except InboundOwnershipConflict:
                pass

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                takeover = pool.submit(_takeover)
                rival = pool.submit(_rival)
                refusal = takeover.result()
                rival.result()
        finally:
            session_map.clear_mirror_links_at = original_clear  # type: ignore[method-assign]

        # The CONFIRMED takeover must win. Serialised, the rival simply arrives after
        # it and is refused. Un-serialised, the rival claims the vacancy the takeover
        # itself opened — so the takeover is refused by its own eviction, and the
        # rival ends up owning a conversation the user handed to someone else.
        assert refusal is None, (
            "the takeover was refused because its own eviction left a vacancy for "
            "the rival to claim"
        )
        owners = session_map.find_mirror_sessions(self.LINK, inbound_only=True)
        assert owners == ["dashboard:chat-2"], (
            f"the confirmed takeover did not win the conversation: owners={sorted(owners)}"
        )


class TestReadersSurviveConcurrentWriters:
    """A reader must not crash because a worker thread is mutating the map.

    Mutators now run on `asyncio.to_thread` and hold `_mutate_lock`; readers hold
    nothing. `_ensure_entry` adds keys to `_data`, so iterating the live top-level
    mapping can raise `RuntimeError: dictionary changed size during iteration` — and
    the reader on the Discord inbound path is `find_mirror_sessions`, so that crash
    drops a user's message rather than merely logging.

    Readers therefore iterate a shallow SNAPSHOT of `_data`. Locking them would also
    be correct but would put a reader on the event loop behind a worker doing file
    I/O in `_save`, which is exactly what moving the writes off the loop avoided.

    Only the top level needs this. `_write_mirrors` installs a NEW bindings dict by
    rebinding `entry["mirrors"]`, so the inner dict a binding reader walks is never
    mutated in place — verified by reverting a snapshot there and finding no test
    could distinguish it, because there is no defect to catch.
    """

    LINK = ChannelLink(channel_type="discord", channel_id="dm-1")

    def test_find_mirror_sessions_does_not_crash_while_keys_are_added(self, session_map):
        import threading
        import time

        session_map.set_mirror_link("dashboard:chat-0", self.LINK, accepts_inbound=True)

        # Hold the iteration OPEN across a real window. Without this the loop over a
        # small dict finishes inside one GIL slice and the race is never lost, so the
        # test passes with or without the snapshot (it did, 5 runs out of 5).
        # `_mirrors` is called once per entry, i.e. inside the loop body.
        original_mirrors = session_map._mirrors

        def _slow_mirrors(entry):
            time.sleep(0.002)
            return original_mirrors(entry)

        session_map._mirrors = _slow_mirrors  # type: ignore[method-assign]

        stop = threading.Event()
        errors: list[BaseException] = []

        def _writer():
            i = 0
            while not stop.is_set() and i < 500:
                # A brand-new key each time: changing the dict SIZE is what makes a
                # live iteration raise.
                session_map._data[f"dashboard:filler-{i}"] = {"sid": ""}
                i += 1
                time.sleep(0.001)

        def _reader():
            try:
                for _ in range(20):
                    session_map.find_mirror_sessions(self.LINK)
            except BaseException as exc:  # noqa: BLE001 - recorded and re-asserted
                errors.append(exc)

        writer = threading.Thread(target=_writer)
        reader = threading.Thread(target=_reader)
        try:
            writer.start()
            reader.start()
            reader.join()
            stop.set()
            writer.join()
        finally:
            session_map._mirrors = original_mirrors  # type: ignore[method-assign]

        assert not errors, f"a reader crashed against a concurrent writer: {errors!r}"

    def test_the_snapshot_is_of_the_mapping_not_a_deep_copy(self, session_map):
        """Cheap by design: entries are shared, only the key list is private.

        A deep copy per read would be a real cost on a hot path. Sharing the entry
        objects is safe because writers replace an entry's bindings by rebinding a
        fresh dict rather than mutating the one a reader holds.
        """
        session_map.set_mirror_link("dashboard:chat-1", self.LINK, accepts_inbound=True)

        entry = session_map._data["dashboard:chat-1"]
        found = session_map.find_mirror_sessions(self.LINK)

        assert found == ["dashboard:chat-1"]
        assert session_map._data["dashboard:chat-1"] is entry

    def test_the_binding_readers_are_safe_too(self, session_map):
        """`_mirrors` is the choke point every binding reader passes through."""
        import threading

        key = "dashboard:chat-1"
        session_map.set_mirror_link(key, self.LINK, accepts_inbound=True)

        stop = threading.Event()
        errors: list[BaseException] = []

        def _writer():
            i = 0
            while not stop.is_set() and i < 400:
                # Same session, different channel types: this mutates the INNER
                # `mirrors` dict that the binding readers walk.
                session_map.set_mirror_link(
                    key, ChannelLink(channel_type=f"ch{i % 7}", channel_id="x")
                )
                i += 1

        def _reader():
            try:
                for _ in range(400):
                    session_map.mirror_accepts_inbound(key)
                    session_map.is_mirror_paused(key, "")
                    session_map.get_mirror_links(key)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        writer = threading.Thread(target=_writer)
        reader = threading.Thread(target=_reader)
        writer.start()
        reader.start()
        reader.join()
        stop.set()
        writer.join()

        assert not errors, f"a binding reader crashed against a writer: {errors!r}"


class TestTheTakeoverIsDurablyAtomic:
    """A confirmed takeover must never be observable ON DISK as a vacancy.

    In-memory atomicity under `_mutate_lock` is not enough: the compound mutator was
    built from primitives that each save, so the eviction was already durable when
    the claim ran. A process that exited in between left the previous binding
    permanently deleted and no new owner — the user loses a link and nobody gains
    one, and unlike the in-memory race a restart does not heal it.
    """

    LINK = ChannelLink(channel_type="discord", channel_id="dm-1")

    def test_the_whole_takeover_lands_in_one_write(self, session_map):
        session_map.set_mirror_link("dashboard:chat-1", self.LINK, accepts_inbound=True)

        # Counted at `os.replace`, the atomic commit point — NOT at `_save`, which is
        # still CALLED by each inner mutator and merely returns early while the
        # deferral is active. Counting calls would report 3 here and prove nothing
        # about how many durable states existed.
        with patch(
            "kiro_crew.session_map.os.replace", side_effect=os.replace
        ) as commits:
            session_map.replace_mirror_owner("dashboard:chat-2", self.LINK)

        assert commits.call_count == 1, (
            f"the takeover committed {commits.call_count} on-disk states; every extra "
            f"one is a state a crash could freeze"
        )

    def test_no_persisted_state_ever_shows_the_conversation_unowned(self, session_map):
        """The property itself, read back from the FILE at every write.

        Stronger than counting writes: it reads what a restarting process would load
        after each save and asserts the conversation always has exactly one owner.
        """
        session_map.set_mirror_link("dashboard:chat-1", self.LINK, accepts_inbound=True)

        seen: list[list[str]] = []
        original = session_map._save

        def _observing_save():
            result = original()
            # Exactly what a fresh process would see.
            on_disk = json.loads(session_map._path.read_text(encoding="utf-8"))
            owners = [
                key for key, entry in on_disk.items()
                if any(
                    b.get("channel_type") == self.LINK.channel_type
                    and b.get("channel_id") == self.LINK.channel_id
                    for b in (entry.get("mirrors") or {}).values()
                )
            ]
            seen.append(owners)
            return result

        session_map._save = _observing_save  # type: ignore[method-assign]
        try:
            session_map.replace_mirror_owner("dashboard:chat-2", self.LINK)
        finally:
            session_map._save = original  # type: ignore[method-assign]

        assert seen, "no write was observed at all"
        for owners in seen:
            assert owners, (
                "a persisted state had the conversation unowned — a crash there loses "
                f"the binding permanently; observed sequence: {seen}"
            )
        assert seen[-1] == ["dashboard:chat-2"]

    def test_a_plain_single_mutator_still_saves_normally(self, session_map):
        """The coalescing must not swallow ordinary writes."""
        with patch(
            "kiro_crew.session_map.os.replace", side_effect=os.replace
        ) as commits:
            session_map.set_mirror_link(
                "dashboard:chat-1", self.LINK, accepts_inbound=True
            )

        assert commits.call_count == 1
        assert session_map._save_depth == 0
        assert session_map._save_pending is False
