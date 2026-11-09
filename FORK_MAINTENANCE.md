# Maintaining the manual-hold fork

Upstream: https://github.com/qlerup/thermostat-pro-timeline

Fork: https://github.com/newmedicine/thermostat-pro-timeline

The fork keeps upstream's `thermostat_timeline` domain and storage keys. Install
and update from the fork repository so releases include the manual-hold patch.
The first fork release is `v3.3.1.post1`, based on upstream v3.3.1 and its current
main-branch fixes. The original MIT licence and credits remain intact.

## Review an upstream update

1. Fetch the upstream remote and merge its latest release/main into a review branch.
2. Resolve conflicts without dropping the manual-target behaviour or upstream fixes.
3. Run `python3 -m unittest discover -s tests -v` and
   `node --test tests/*.test.cjs`. The existing regression workflow runs both in CI.
4. Verify target restoration, delayed startup reconciliation, and a timed room resume
   against Home Assistant before publishing.
5. Update `manifest.json` to the new fork release version, merge the review branch
   into `main`, and publish a matching tag/release.

The inherited release workflow builds `thermostat-pro-timeline-sync.zip`, including
the integration contents and bundled frontend. Marketplace/HACS uses that asset.
Do not enable unattended upstream merges or direct upstream integration updates:
upstream changes can need a reviewed adaptation of the patch.

## Return to upstream

Once an upstream release includes equivalent behaviour, back up Timeline's
schedules/settings, switch the downloaded repository back to upstream, and restart
Home Assistant. Preserve the existing config entry and domain. Generated timed
room pauses can expire normally or be cancelled in the Timeline card.
