"""Visitor statistics of the static website, built from Caddy's access log.

The package runs inside short-lived containers without network access (except
the monthly geodata download): it separates people from programs, keeps only
pseudonymous data beyond the raw log's seven days, and renders static pages.
"""
