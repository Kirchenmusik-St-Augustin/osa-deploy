#!/bin/sh
# Renders the two detail reports of the visitor statistics with GoAccess:
# menschen.html (people) and automatisch.html (automated access). The
# dashboard itself comes from the filter (python -m augustiner_stats run),
# which always finishes before this script starts (Requires=/After= in
# augustiner-stats-report.container) and is the only writer of its input.
#
# A full analysis of 25 months would take close to a minute every hour, so
# GoAccess works incrementally:
# - Every day the filter closes is named in state/goaccess-queue. Those days
#   are added to one GoAccess database per report (--persist), oldest first.
# - Every run then renders both reports from the database plus the lines of
#   the day in progress (current/), without storing them.
# GoAccess counts a day it is given twice twice, so each database remembers
# the last day it holds (goaccess/<report>.last-day) and takes only later
# ones. A run that fails while adding days keeps its claim on them and
# repeats them next time; the days stored before the failure are skipped.
#
# Environment: STATS_DATA (the data directory of the statistics) and
# STATS_ASSETS (the directory holding automatisch.js), both required.
set -eu

: "${STATS_DATA:?STATS_DATA is not set}"
: "${STATS_ASSETS:?STATS_ASSETS is not set}"

# Keep exactly what the filter keeps: people's days for 25 months, automated
# access for 90 days (PEOPLE_DAYS and PROGRAM_DAYS in augustiner_stats).
people_days=775
program_days=90
# The line format of the filter (goaccess_line in augustiner_stats.days).
log_format='%v %h %e [%d:%t] "%r" %s %b "%R" "%u" %D'

queue="$STATS_DATA/state/goaccess-queue"
incoming="$queue.incoming"
claimed="$queue.claimed"
days="$STATS_DATA/days"
current="$STATS_DATA/current"
geo="$STATS_DATA/geo"
www="$STATS_DATA/www"
# Databases and half-written files; outside www/, so the web server never
# shows a report before it is complete.
work="$STATS_DATA/goaccess"

fail() {
	printf 'augustiner-stats-report: %s\n' "$*" >&2
	exit 1
}

# $1: the report (menschen or automatisch); then the input and the options of
# this call.
analyse() {
	report=$1
	shift
	if [ "$report" = menschen ]; then
		# GoAccess's own crawler list as a second, independent filter. The
		# address and the remote-user field say nothing about people.
		set -- "$@" --ignore-crawlers --ignore-panel=REMOTE_USER \
			"--keep-last=$people_days" '--html-report-title=Besucher im Detail'
	else
		# The filter writes the kind of automated access into the
		# remote-user field; automatisch.js renames that panel.
		set -- "$@" "--keep-last=$program_days" --html-custom-js=automatisch.js \
			'--html-report-title=Automatische Zugriffe'
	fi
	# Without the geodata job's databases the reports simply lack the
	# country and network panels.
	for database in country asn; do
		if [ -f "$geo/$database.mmdb" ]; then
			set -- "$@" "--geoip-database=$geo/$database.mmdb"
		fi
	done
	mkdir -p "$work/$report"
	goaccess "$@" --no-global-config --no-progress \
		"--log-format=$log_format" --date-format=%Y-%m-%d --time-format=%H:%M:%S \
		--ignore-panel=HOSTS --restore "--db-path=$work/$report"
}

# Takes over the days the filter has queued since the last run. Days claimed
# by a failed run stay claimed and are added again.
claim_queue() {
	if [ -f "$queue" ]; then
		mv "$queue" "$incoming"
	fi
	if [ -f "$incoming" ]; then
		cat "$incoming" >>"$claimed"
		rm "$incoming"
	fi
}

# $1: a day as YYYY-MM-DD; prints it as a number that sorts the same way.
day_number() {
	echo "$1" | tr -d -
}

# $1: the day, $2: the report.
add_day() {
	file="$days/$1.$2.log.gz"
	last_day="$work/$2.last-day"
	if [ -f "$last_day" ] &&
		[ "$(day_number "$1")" -le "$(day_number "$(cat "$last_day")")" ]; then
		echo "$2 already holds $1 or a later day, skipped"
		return 0
	fi
	if [ ! -f "$file" ]; then
		echo "no $2 lines kept for $1, skipped"
		return 0
	fi
	# zcat's exit status is lost in the pipe, so the file is checked first.
	gzip -t "$file" || fail "damaged file ${file}"
	zcat "$file" | analyse "$2" - --persist --process-and-exit
	echo "$1" >"$last_day.tmp"
	mv "$last_day.tmp" "$last_day"
	echo "added $1 to the $2 report"
}

add_claimed_days() {
	[ -f "$claimed" ] || return 0
	sort -u -o "$claimed" "$claimed"
	# Read through descriptor 3, so no command in the loop can consume the
	# list from standard input.
	while IFS= read -r day <&3; do
		case $day in
		[0-9][0-9][0-9][0-9]-[0-1][0-9]-[0-3][0-9]) ;;
		*) fail "unexpected entry in the queue: ${day}" ;;
		esac
		add_day "$day" menschen
		add_day "$day" automatisch
	done 3<"$claimed"
	rm "$claimed"
}

# $1: the report.
render() {
	input="$current/$1.log"
	[ -f "$input" ] || input=/dev/null
	analyse "$1" "$input" "--output=$work/$1.html"
	mv "$work/$1.html" "$www/$1.html"
}

publish_script() {
	cp "$STATS_ASSETS/automatisch.js" "$work/automatisch.js"
	mv "$work/automatisch.js" "$www/automatisch.js"
}

mkdir -p "$(dirname "$queue")" "$www" "$work"
claim_queue
add_claimed_days
publish_script
render menschen
render automatisch
echo "rendered the detail reports"
