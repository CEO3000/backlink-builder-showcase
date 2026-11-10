"""Backlink publishing: finds places a relevant page can legitimately be published, writes the content,
submits it through a supported route, and independently verifies that the backlink is live.

This is separate from the email outreach in src/outreach.py and src/venue_campaign.py, which it never touches.
Entry point: python -m src.backlinks.pipeline
"""
