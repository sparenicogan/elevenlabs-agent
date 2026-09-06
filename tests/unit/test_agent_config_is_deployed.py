"""The agent's configuration reaches ElevenLabs the way the Lambdas reach AWS.

The prompt and the tool definitions are the security boundary the model actually reads. A
change made in the dashboard leaves no trace, passes no review, and is silently reverted by
the next sync -- so the pipeline has to be the thing that applies them, and these tests hold
that wiring in place.
"""

import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
AGENT = json.loads((ROOT / "agent" / "agent.json").read_text())
DEPLOY = (ROOT / ".github" / "workflows" / "main.yml").read_text()


class TestThePipelineSyncsTheAgent:
    def test_the_deploy_workflow_runs_the_sync(self):
        assert "scripts.agent.sync" in DEPLOY

    def test_it_syncs_after_terraform_applies(self):
        """The tool URLs come from terraform outputs, so an endpoint has to exist before the
        agent is pointed at it."""
        assert DEPLOY.index("terraform apply") < DEPLOY.index("scripts.agent.sync")

    def test_the_agent_id_lives_in_the_repository(self):
        """Not passed on a command line, where two callers can disagree about which agent
        they are configuring."""
        assert AGENT["agent_id"].startswith("agent_")


class TestRetentionIsDeclaredNotClicked:
    """Transcripts live at ElevenLabs rather than in our own store, so their retention is not
    a convenience setting -- it is the only thing expiring them, and it belongs under review.
    The S3 lifecycle rule that used to do this job is gone with the bucket."""

    def test_retention_is_declared_in_the_repository(self):
        assert AGENT["platform_settings"]["privacy"]["retention_days"] == 90

    def test_it_is_a_definite_period_rather_than_for_ever(self):
        """The workspace default was -1, which is unlimited."""
        assert AGENT["platform_settings"]["privacy"]["retention_days"] > 0


class TestTheSyncOnlyOverridesWhatWeDeclare:
    def test_settings_we_have_no_opinion_about_survive(self):
        """agent.json declares a handful of keys against a settings object with about thirty.
        Sending the subset alone would drop the rest."""
        from scripts.agent.sync import merged_platform_settings

        merged = merged_platform_settings(
            {"privacy": {"retention_days": 3650}},
            {"privacy": {"retention_days": -1, "record_voice": False}, "widget": {"x": 1}},
        )
        assert merged["privacy"]["retention_days"] == 3650
        assert merged["privacy"]["record_voice"] is False
        assert merged["widget"] == {"x": 1}
