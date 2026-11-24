#!/bin/sh
# Mirrors one branch of the public static website repository of the
# Augustinian church into AUG_TARGET. augustiner-pull.timer starts it every few
# minutes in a short-lived container (augustiner-pull.container); Caddy serves
# the result from a read-only mount.
#
# The repository is maintained by a third party, so its content is not
# trusted:
# - Symbolic links are never checked out (core.symlinks=false), and a revision
#   that contains one is refused before any of its files is written. Caddy
#   follows links, and one pointing at /data would publish the TLS keys.
# - Nothing starts while the disk is nearly full, so a huge commit cannot
#   starve the database that shares the disk.
# - The copy always equals the remote branch (fetch + reset --hard): a
#   force-push upstream is followed instead of blocking every later update.
#   Nothing writes into the copy except this script.
#
# Environment: AUG_REPO_URL, AUG_TARGET, AUG_MIN_FREE_MB (required),
# AUG_BRANCH (default: main).
set -eu

: "${AUG_REPO_URL:?AUG_REPO_URL is not set}"
: "${AUG_TARGET:?AUG_TARGET is not set}"
: "${AUG_MIN_FREE_MB:?AUG_MIN_FREE_MB is not set}"
branch="${AUG_BRANCH:-main}"
parent=$(dirname "$AUG_TARGET")

fail() {
	printf 'augustiner-pull: %s\n' "$*" >&2
	exit 1
}

require_free_space() {
	free_mb=$(df -Pm "$parent" | awk 'NR == 2 { print $4 }')
	[ "$free_mb" -ge "$AUG_MIN_FREE_MB" ] && return 0
	fail "only ${free_mb} MB free below ${parent}, at least ${AUG_MIN_FREE_MB} MB required"
}

# $1: repository directory, $2: revision to inspect before it is checked out.
refuse_symlinks() {
	links=$(git -C "$1" ls-tree -r "$2" | awk '$1 == "120000" { print $4 }')
	[ -z "$links" ] && return 0
	fail "refusing revision $(git -C "$1" rev-parse --short "$2"), it contains symbolic links: $(echo "$links" | tr '\n' ' ')"
}

# Clones into a staging directory and renames it at the end, so the web server
# never sees a half-written copy.
first_clone() {
	staging="${AUG_TARGET}.tmp"
	rm -rf "$staging"
	git clone --quiet --no-checkout --single-branch --branch "$branch" \
		--config core.symlinks=false "$AUG_REPO_URL" "$staging"
	refuse_symlinks "$staging" HEAD
	git -C "$staging" reset --quiet --hard HEAD
	mv "$staging" "$AUG_TARGET"
	echo "cloned $(git -C "$AUG_TARGET" rev-parse --short HEAD)"
}

update() {
	# Re-applied on every run, in case the copy was created by other means.
	git -C "$AUG_TARGET" config core.symlinks false
	before=$(git -C "$AUG_TARGET" rev-parse HEAD)
	git -C "$AUG_TARGET" fetch --quiet --prune origin "$branch"
	after=$(git -C "$AUG_TARGET" rev-parse FETCH_HEAD)
	if [ "$before" = "$after" ]; then
		echo "unchanged at $(git -C "$AUG_TARGET" rev-parse --short HEAD)"
		return 0
	fi
	refuse_symlinks "$AUG_TARGET" FETCH_HEAD
	git -C "$AUG_TARGET" reset --quiet --hard FETCH_HEAD
	echo "updated $(git -C "$AUG_TARGET" rev-parse --short "$before") -> $(git -C "$AUG_TARGET" rev-parse --short HEAD)"
}

require_free_space
if [ ! -d "$AUG_TARGET/.git" ]; then
	first_clone
	exit 0
fi
update
