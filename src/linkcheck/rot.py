"""Heuristics for "webpage rot": links that answer 200 but no longer lead anywhere
useful - a redirect dumped on the site's homepage, a parked/for-sale domain, or a
soft-404 page that never bothered to send a real 404.

Precision over recall, deliberately. This project has been burned before by
overeager "broken" detection (AIA chasing, SECLEVEL, lenient header parsing, 429
handling - see notes.md/CLAUDE.md) where a link reported broken turned out to be
fine, costing a human's review time. A link that's actually rotten but goes
undetected here just persists as today's status quo - no worse than before this
module existed. So every heuristic below is written to accept some missed rot
rather than risk flagging a link that's still genuinely useful, and each one is a
pure function of plain strings (never touches the network or a live response) so
it can be run offline against captured sweep data while tuning it.

detect_rot() composes the heuristics below, in order: parking, hijacked, soft_404,
homepage_redirect, media_replaced, auth_wall (order only matters for which reason
slug wins when more than one would match, which is rare in practice - phrase sets
are chosen to barely overlap). Only ever meaningful for a 2xx response with
error_type unset; detect_rot itself guards on http_status, but classify() in
checker.py only calls it in that case anyway. Each heuristic is its own standalone
pure function, so adding a future one (e.g. a library-backed spam/off-domain-redirect
check) is just one more function plus one more line in detect_rot's chain - nothing
else to restructure.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote, urlsplit

REASON_PARKING = "parking"
REASON_HIJACKED = "hijacked"
REASON_SOFT_404 = "soft_404"
REASON_HOMEPAGE_REDIRECT = "homepage_redirect"
REASON_MEDIA_REPLACED = "media_replaced"
REASON_AUTH_WALL = "auth_wall"
REASON_VIDEO_UNAVAILABLE = "video_unavailable"

# A shortened link's entire path is an opaque token with no meaning of its own - if
# it resolves to some site's bare homepage, that may be exactly what the shortener
# was configured to point at, not evidence of rot. Kept short and specific rather
# than exhaustive; an unlisted shortener just falls through to the normal checks.
SHORTENER_HOSTS = frozenset(
    {
        "bit.ly", "tinyurl.com", "goo.gl", "t.co", "ow.ly", "is.gd", "buff.ly",
        "youtu.be", "amzn.to", "wp.me", "forms.gle", "tiny.cc", "rebrand.ly",
        "cutt.ly", "shorturl.at", "bit.do", "lnkd.in",
    }
)

# Second-level labels that, paired with a 2-letter ccTLD (co.uk, com.au, ...), mean
# the *third*-from-last label is the actual registrable boundary rather than the
# second - not an exhaustive PSL, just enough to keep the parking/spam host checks
# from mis-truncating the common ccTLD-second-level combinations they're likely to
# see in practice (see _registrable_domain).
_CCTLD_SECOND_LEVEL_LABELS = frozenset({"co", "com", "net", "org", "ac", "gov", "edu"})


def _registrable_domain(host: str) -> str:
    """Best-effort eTLD+1 without a Public Suffix List dependency: the last two
    labels, or the last three when the second-to-last label is one of the common
    ccTLD-paired second-level labels above (co.uk, com.au, ...). Doesn't need to be
    perfect - it only gates the parking-host heuristic and www-style normalization,
    never anything that decides a link is fine.
    """
    host = host.lower().rstrip(".")
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    tld, second = labels[-1], labels[-2]
    if len(tld) == 2 and second in _CCTLD_SECOND_LEVEL_LABELS:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


# WordPress/Squarespace (and most other modern CMSes) emit a curly right single
# quote (U+2019) rather than an ASCII apostrophe in generated copy - "page can't be
# found" ships as "page can’t be found" - so every phrase list below that
# contains an apostrophe would otherwise never match the platforms it's meant to
# catch. U+2018 (curly left single quote), U+02BC (modifier letter apostrophe,
# common in some CMS export pipelines) and U+00B4 (acute accent, visually similar
# and sometimes substituted for an apostrophe by lossy encoding conversions) are
# normalized the same way.
_APOSTROPHE_TRANSLATION = str.maketrans("’‘ʼ´", "''''")


def _normalize_apostrophes(text: str | None) -> str | None:
    return text.translate(_APOSTROPHE_TRANSLATION) if text else text


def _default_port(scheme: str) -> int | None:
    if scheme == "http":
        return 80
    if scheme == "https":
        return 443
    return None


def _comparable_parts(parsed) -> tuple[str, int | None, str, str]:
    """hostname (lowercased) + normalized port + path + query - the "same place"
    comparison shared by _redirected and homepage_redirect. Deliberately not
    `.netloc`: httpx's response URLs already normalize away a default port
    (":80"/":443") and host case, but a URL authored with an *explicit* default
    port (http://example.com:80/...) does not get that normalization applied to
    it - comparing raw netloc then sees "example.com:80" != "example.com" and
    concludes a redirect happened when nothing actually changed. Scheme is
    excluded on purpose: an https-upgrade is never itself "a redirect happened"
    for these heuristics.
    """
    host = (parsed.hostname or "").lower()
    port = parsed.port or _default_port(parsed.scheme)
    return (host, port, parsed.path, parsed.query)


def _redirected(url: str, final_url: str | None) -> bool:
    """True when final_url reflects an actual redirect away from url, using the same
    scheme-insensitive host/path/query comparison as homepage_redirect below - an
    https-upgrade or bare www-canonicalization alone never counts as "a redirect
    happened" for the heuristics that require one (soft_404's error-path case,
    auth_wall).
    """
    if not final_url:
        return False
    orig = urlsplit(url)
    final = urlsplit(final_url)
    return _comparable_parts(orig) != _comparable_parts(final)


# Drop-catch/hacked-site SEO spam ("this domain died and now serves gambling/pharma
# spam instead"). Same host, no redirect involved - homepage_redirect and parking
# can't see this at all, so it needs its own check. Title only, deliberately: a body-
# only match in the validation sweep was always either a spam snippet injected into a
# page still serving its real content, or ordinary user-generated content on a
# healthy platform - neither is the "the whole page is now spam" failure this guards.
# Mostly Indonesian gambling-SEO vocabulary (which cannot appear in genuine English
# curriculum titles) plus two pharma staples that show up on the same kind of
# hijacked site. "slot gacor" was dropped as its own entry - bare "gacor" alone
# already covers it and every other "gacor"-suffixed variant.
#
# Deliberately rejected tokens, and why they stay out (each is a real false-positive
# mode found while tuning this list, not an oversight): "pragmatic play" (a speech-
# development term that appears in this population's own curriculum content, not
# just the game studio of the same name); bare "judi" (a real given name); bare
# "situs" (a genuine Latin/medical term); "casino" (turns up in ordinary health-unit
# prose, e.g. gambling-addiction curriculum); "toto" (a band name and a common dog
# name, both of which show up in real linked content).
_HIJACKED_TITLE_TOKENS = (
    "gacor", "situs slot", "situs judi", "judi online", "judi slot", "togel",
    "sbobet", "ufabet", "maxwin", "slot88", "bandar judi", "bokep", "rtp slot",
    "slot online", "cialis", "viagra", "terpercaya", "judi bola", "agen judi",
    "pkv games", "bandarq", "dominoqq", "idn poker", "scatter hitam",
    "gampang menang", "levitra",
)

# \b-anchored rather than a plain substring search - "cialis" as a bare substring
# false-positived on "specialist" 300+ times in the validation sweep ("...cialis..."
# sits inside "spe-cialis-t"), which \b rules out since there's no word boundary
# between the "e" and the "c" there.
_HIJACKED_TITLE_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(token) for token in _HIJACKED_TITLE_TOKENS) + r")\b",
    re.IGNORECASE,
)


def hijacked(page_title: str | None) -> bool:
    """True when a 200 response's title reads like a drop-catch/hacked site's SEO
    spam rather than genuine page content."""
    page_title = _normalize_apostrophes(page_title)
    if not page_title:
        return False
    return bool(_HIJACKED_TITLE_RE.search(page_title))


# The classic "content removed, server 200s everything to /" rot mode - the link
# still "works" in the sense of returning 200, but every redirect lands on the
# site's front page instead of the page that was actually linked.
#
# Anchored regex (both sides of the comparison run through _is_homepage_path
# below) rather than a fixed set of literal paths plus an unanchored
# startswith("/index.") - the unanchored prefix check used to exempt MediaWiki/
# Joomla path-info URLs like "/index.php/2012/10/08/how-bird-wings-work/" from
# this heuristic entirely, since they too start with "/index." even though the
# path continues well past it into real content. Anchoring with $ after an
# optional short extension fixes that while still matching the common homepage
# spellings ("/index.html", "/index.php", "/home", and now "/default.aspx"-style
# root documents too).
_HOMEPAGE_PATH_RE = re.compile(r"^/(?:index|default|home)(?:\.[a-z0-9]{1,5})?$", re.IGNORECASE)


def _is_homepage_path(path: str) -> bool:
    stripped = path.rstrip("/")
    return not stripped or bool(_HOMEPAGE_PATH_RE.match(stripped))


def homepage_redirect(url: str, final_url: str | None) -> bool:
    """True when a redirect landed on a homepage that the original link clearly
    wasn't pointing at.

    Deliberately narrow to avoid the much more common, perfectly benign case of a
    `?utm=...`-stripping redirect from a query-only URL to a site's bare homepage:
    only a link whose ORIGINAL path (not just query string) was meaningful counts -
    which also rules out the original URL already being that homepage, since then
    it would have no meaningful path to begin with. Same-host and cross-host
    redirects both count: a parked domain that 302s example.com/deep-lesson to
    parkedsite.com/ lands here too if the parking heuristic's own body-text/host
    signals happen to miss it.
    """
    if not final_url:
        return False
    orig = urlsplit(url)
    final = urlsplit(final_url)
    # Scheme is never meaningful to any of these comparisons (an https-upgrade or a
    # www-canonicalization redirect isn't rot) - only host/path/query identify
    # "the same place". Port is normalized the same way _redirected() does (see
    # _comparable_parts) so an explicit ":80"/":443" in the original URL doesn't
    # read as a redirect when nothing actually changed.
    if _comparable_parts(orig) == _comparable_parts(final):
        return False  # nothing actually changed - not a redirect at all

    if not orig.path.rstrip("/"):
        return False  # query-only or already-bare original: no meaningful path to lose

    # If the ORIGINAL path was already a homepage variant itself (e.g. "/home",
    # "/index.tmpl") then landing on the bare root is just canonicalization to one
    # canonical homepage spelling, not content going missing.
    if _is_homepage_path(orig.path):
        return False

    if final.query:
        return False  # e.g. a redirect to "/?ref=expired" isn't a bare homepage

    if not _is_homepage_path(final.path):
        return False

    if _registrable_domain(_host(url)) in SHORTENER_HOSTS:
        return False  # the shortener's whole path is opaque; landing on a homepage may be the point

    # A redirect from a bare host/www to its own dedicated subdomain root (content
    # moved to live at its own subdomain rather than under the main site) is a move,
    # not a removal - only flag when the final host isn't itself just a deeper
    # descendant of the original host. Compared after stripping a single leading
    # "www." from each side, since that alone is never meaningful here (already
    # handled above); same-host (after that stripping) is still eligible below, only
    # a genuine parent-to-child relationship is exempted.
    orig_host = _host(url).removeprefix("www.")
    final_host = _host(final_url).removeprefix("www.")
    if orig_host and final_host.endswith("." + orig_host):
        return False

    return True


# WordPress's default themes title a dead page "Page not found – <Site Name>"; cPanel
# suspends an account with a literal "Account Suspended" title; these two alone
# account for most soft-404s seen in practice on both curriculum sites.
SOFT_404_TITLE_PHRASES = (
    "404 not found",
    "error 404",
    "404 error",
    "not found | ",
    "page doesn't exist",
    "page does not exist",
    "page can't be found",
    "page cannot be found",
    "page unavailable",
    "page no longer exists",
    "page has been removed",
    "page you requested",
    "oops! that page",
    "account suspended",
)

# The "<noun> not found"-shaped titles ("page not found", "video not found", "site
# not found", ... "page was not found") used to be individual entries in the phrase
# list above - folded into one regex instead so the same shape also covers nouns
# that weren't previously listed (post/article/resource/content/record/entry) and
# phrasing variants ("was/is/has been not found", "could n't be found", "no longer
# exists/available", "does n't exist") without enumerating every noun x phrasing
# combination by hand. Title-only, same as the strings it replaces.
_SOFT_404_NOT_FOUND_TITLE_RE = re.compile(
    r"\b(?:page|post|article|video|item|product|document|file|resource|content|record|entry|site)"
    r"\s+(?:was\s+|is\s+|has\s+been\s+)?"
    r"(?:not found|could ?n[o']t be found|cannot be found|can't be found|"
    r"no longer (?:exists|available)|does ?n[o']t exist|unavailable|removed)\b"
)

# A title consisting of ONLY one of these (nothing else) is unambiguous even
# without a specific phrase - anything more descriptive is checked against the
# phrase list above instead, since a bare content page could coincidentally be
# titled "Error" as part of something else.
SOFT_404_EXACT_TITLES = frozenset({"404", "not found", "error"})

# Server/software default landing pages left up where a real page used to be - a
# blank webroot after the original content was pulled, rather than anything
# app-specific returning its own 404. Prefix (not substring) match: these are the
# opening words of the stock title these servers/panels ship with, so a prefix is
# both sufficient and specific enough not to false-positive on unrelated content.
# "index of /" also catches a bare Apache/nginx directory listing standing in for a
# page that used to be there (e.g. biblehub.com/childrens/ -> "Index of /childrens").
SOFT_404_TITLE_PREFIXES = (
    "welcome to nginx",
    "apache2 ubuntu default",
    "apache http server test page",
    "iis windows server",
    "default web site page",
    "index of /",
)

# A redirect that lands on a URL whose path is itself an error-page slug - the
# server's own routing already told us this was a 404, just via a 200 rather than a
# real 404 status. Anchored to the LAST path segment (rather than a fixed set of
# exact paths) so a deeper route ending in an error-page slug still counts (e.g.
# "/en/404"), while requiring the match to consume the segment entirely (via $)
# rules out a slug merely appearing as a substring of an unrelated segment (e.g.
# "/lesson-404-review" must not match - "404" there is just a lesson number, not a
# server routing straight to its own error page).
SOFT_404_ERROR_PATH_RE = re.compile(
    r"(?:^|/)(?:404|not[-_]?found|page[-_]?not[-_]?found|error[-_]?404|404[-_]?(?:page|error))"
    r"(?:\.[a-z0-9]{1,5})?$",
    re.IGNORECASE,
)


def _redirected_to_error_path(url: str, final_url: str | None) -> bool:
    if not _redirected(url, final_url):
        return False
    final_path = urlsplit(final_url).path.rstrip("/")
    return bool(SOFT_404_ERROR_PATH_RE.search(final_path))


# Full sentences, not fragments - kept deliberately whole (rather than reduced to
# fragments like "no longer exists") so a legitimate page mentioning "404" or
# "removed" as ordinary content doesn't trip this. Blogger's own hosted-blog-deleted
# page is the source of the "blog you were looking for" phrasing; "website expired"
# and "this site is currently unavailable" are common registrar/host interstitials
# (GoDaddy-style expiry pages, Weebly/Wix-style deactivated sites).
SOFT_404_BODY_PHRASES = (
    "the page you requested could not be found",
    "the page you are looking for does not exist",
    "the page you're looking for doesn't exist",
    "the page you are looking for no longer exists",
    "the page you were looking for could not be found",
    "this page has been removed",
    "this content is no longer available",
    "sorry, this page isn't available",
    "the blog you were looking for does not exist",
    "this website has been suspended",
    "this account has been suspended",
    "website expired",
    "this site is currently unavailable",
)


def soft_404(url: str, final_url: str | None, page_title: str | None, body_excerpt: str | None) -> bool:
    """True when a 200 response's own title (or, failing that, its body) describes
    itself as a not-found/suspended/expired page, is a server/software default
    landing page standing in for one, or landed - via a redirect - on a URL whose
    own path is an error-page slug.

    Title-first and title-weighted on purpose: a page's title is its own
    self-description, so it's much harder for legitimate content to trip a title
    phrase by accident than a body phrase - "the story of the 404 boys' brigade"
    would be a false positive under a loose body-text match but the specific title
    phrases here ("page not found", "error 404", ...) don't match ordinary prose
    the way a bare "404" substring would. Body phrases are checked too, but kept to
    long, specific sentences for the same reason. `url`/`final_url` are only needed
    for the error-path-redirect case; every other case here never touches them.
    """
    page_title = _normalize_apostrophes(page_title)
    body_excerpt = _normalize_apostrophes(body_excerpt)
    if page_title:
        title = page_title.strip().lower()
        if title in SOFT_404_EXACT_TITLES:
            return True
        if any(phrase in title for phrase in SOFT_404_TITLE_PHRASES):
            return True
        if _SOFT_404_NOT_FOUND_TITLE_RE.search(title):
            return True
        if title.startswith(SOFT_404_TITLE_PREFIXES):
            return True
    if body_excerpt:
        excerpt = body_excerpt.lower()
        if any(phrase in excerpt for phrase in SOFT_404_BODY_PHRASES):
            return True
    if _redirected_to_error_path(url, final_url):
        return True
    return False


# Registrable domains of well-known parking/domain-marketplace services. porkbun.com
# itself is deliberately excluded - it's a real registrar whose main site people
# legitimately link to; only its parked-domain landing subdomain (see
# _is_parked_label_host) is a parking signal. godaddy.com is excluded for the same
# reason - its parked pages are still caught by the body phrases below.
PARKING_HOSTS = frozenset(
    {
        "sedoparking.com", "sedo.com", "parkingcrew.net", "bodis.com",
        "hugedomains.com", "dan.com", "afternic.com", "sav.com", "above.com",
        "buydomains.com", "domainmarket.com", "dnparking.com", "cashparking.com",
        "parklogic.com", "undeveloped.com", "brandbucket.com", "squadhelp.com",
        "atom.com", "expireddomains.com",
    }
)


def _is_parked_label_host(host: str) -> bool:
    """True when host's leftmost label is exactly "parked" - catches
    parked.<any-registrar> (parked.porkbun.com, parked.namecheap.com, ...) without
    needing an exact-host entry per registrar. Deliberately an exact label match,
    not a prefix/substring one: a "parking." subdomain (e.g. a university's real
    visitor-parking-info page at parking.example.edu) is a genuine link in this
    population and must never be caught by this.
    """
    return host.split(".")[0] == "parked"


# Bare "for sale"/"is for sale" is deliberately excluded - plenty of real course-
# linked content (a used-book marketplace, a curriculum store) legitimately sells
# things. Only phrases that specifically name the *domain itself* as the thing for
# sale/parked/expired qualify. "related searches" (a parking-page staple) is
# excluded too - it's generic enough to appear on real search-result-style pages.
PARKING_BODY_PHRASES = (
    "make an offer on this domain",
    "domain is parked",
    "parked domain",
    "domain parking",
    "parked free, courtesy of godaddy",
    "this domain has expired",
    "domain expired",
    "renew this domain",
)

# "this/the/that domain (name) is/may be/might be for sale" - folds what used to be
# four separate literal strings ("this domain is for sale", "domain may be for
# sale", "domain name is for sale", "the domain is for sale") into one pattern.
# "domain" is still required to be the grammatical subject, same as the strings it
# replaces - "this book is for sale" must not match.
_PARKING_FOR_SALE_RE = re.compile(
    r"\b(?:this |the |that )?domain(?: name)? (?:is|may be|might be) for sale\b", re.IGNORECASE
)

# "buy/purchase/get/acquire this domain" - folds what used to be three separate
# literal strings ("buy this domain", "purchase this domain", "get this domain")
# into one pattern, plus "acquire" which wasn't previously listed.
_PARKING_BUY_DOMAIN_RE = re.compile(r"\b(?:buy|purchase|get|acquire) this domain\b", re.IGNORECASE)


def parking(final_url: str | None, page_title: str | None, body_excerpt: str | None) -> bool:
    """True when the final URL's host is a known parking/marketplace service, or the
    page's own title+body describe it as parked/for-sale/expired.

    Title and body are tested separately, never concatenated into one haystack - a
    phrase must appear whole within one or the other, not stitched together across
    the title/body seam (e.g. a title ending "...buy this" followed by a body
    starting "domain today..." must not read as "buy this domain").
    """
    if final_url:
        host = _host(final_url)
        if _is_parked_label_host(host) or _registrable_domain(host) in PARKING_HOSTS:
            return True
    for text in (_normalize_apostrophes(page_title), _normalize_apostrophes(body_excerpt)):
        if not text:
            continue
        text = text.lower()
        if any(phrase in text for phrase in PARKING_BODY_PHRASES):
            return True
        if _PARKING_FOR_SALE_RE.search(text) or _PARKING_BUY_DOMAIN_RE.search(text):
            return True
    return False


# A URL that plainly names a downloadable file (a worksheet PDF, a photo, a slide
# deck, a video/audio clip) but now answers with an HTML page instead - a hosting
# migration or CMS swap that serves a generic page (or a "file not found" page too
# generic for soft_404's phrase list) at the old direct-file URL rather than the
# file itself. Deliberately does NOT include txt/zip/xlsx/epub - a file-host
# download-interstitial page (a generic "your download will begin shortly" page
# with no title overlap) is a plausible false-positive mode for exactly those
# extensions, so they're left out rather than risk it. The Wikipedia-File:-page
# guard in media_replaced() below applies identically to every extension here -
# a "File:Foo.svg" page still names the file in its own title.
MEDIA_EXTENSION_RE = re.compile(
    r"\.(pdf|jpe?g|png|gif|mp3|mp4|docx?|pptx?|ogg|oga|webp|svg|wav|mov|avi|wmv|webm)$",
    re.IGNORECASE,
)

# Split on runs of non-alphanumerics rather than just "-"/"_" - filenames and titles
# use a mix of separators (dashes, underscores, colons, spaces) that all mean the same
# thing here. Words under 4 chars are dropped as too likely to coincide by chance
# ("The", "of", file extensions echoed into the title); purely-numeric "words" are
# dropped too since archive.org's own mp3 stems are bare item numbers (e.g.
# "28733-10") that must never count as meaningful, matching or not.
_WORD_SPLIT_RE = re.compile(r"[^a-z0-9]+")


def _significant_words(text: str) -> set[str]:
    return {
        word
        for word in _WORD_SPLIT_RE.split(text.lower())
        if len(word) >= 4 and not word.isdigit()
    }


def media_replaced(url: str, page_title: str | None) -> bool:
    """True when url's path names a non-HTML media file but the response that came
    back had an HTML title sharing no significant word with that filename.

    Word-overlap, not "any HTML at a media URL", because plenty of legitimate
    services front a media file with an HTML page that still names the file: a
    Wikipedia/Wikimedia "File:" page and a kiddle.co "Image:" page both title
    themselves after the exact filename, and archive.org's "details" pages for an
    .mp3 always mention the work's own title even though the bare item-number stem
    itself (see _significant_words) never overlaps with anything.
    """
    if page_title is None:  # title is only ever extracted from an HTML response
        return False
    parsed = urlsplit(url)
    filename = parsed.path.rsplit("/", 1)[-1]
    match = MEDIA_EXTENSION_RE.search(filename)
    if not match:
        return False
    # archive.org's "/details/<item>[/<sub-file>]" page is a live viewer for one
    # file WITHIN a multi-file item, titled after the item as a whole (e.g.
    # ".../details/AnimatedHeroClassics/William+Bradford.avi" is titled "Animated
    # Hero Classics : ... : Internet Archive") - the sub-file's own name
    # legitimately never appears in that title, by construction, regardless of
    # word overlap. The pre-existing .mp3 case in this population only escaped
    # this same FP mode by luck (a purely-numeric item-number stem, see
    # _significant_words) - this is the actual guard for it.
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if host == "archive.org" and parsed.path.startswith("/details/"):
        return False
    stem_words = _significant_words(unquote(filename[: match.start()]))
    if not stem_words:  # nothing in the filename itself was significant enough to check
        return False
    return stem_words.isdisjoint(_significant_words(page_title))


# A login page's path is normally just "/login" or "/sign-in" (optionally under a
# subpath, or with an arbitrary prefix glued onto the same final segment - "/wp-
# login.php", "/ServiceLogin"). Any word ending in "-login"/"-signin" is, in
# practice, always an auth route, so the generic pattern below already covers
# Rails-style apps' default devise route ("/users/sign_in") - no special case
# needed for it anymore. Anchored to the LAST path segment (with an optional file
# extension) so "log"/"sign" merely appearing as a substring elsewhere in the path
# doesn't count - "plugin", "checkin", "design", and "signing-an-apartment-lease"
# must not match, since none of them actually END their last segment in "login" or
# "signin".
_LOGIN_PATH_RE = re.compile(r"(^|/)[a-z0-9_-]*(?:log|sign)[-_]?in(\.\w+)?/?$", re.IGNORECASE)

_LOGIN_TITLE_PHRASES = ("login", "log in", "sign in", "sign-in", "signin")


def auth_wall(url: str, final_url: str | None, page_title: str | None) -> bool:
    """True when a redirect landed on what looks like a login page by BOTH its path
    and its title.

    Path alone isn't enough - ibiblio.org/wm/paint/auth/grunewald/ false-positived on
    a path-only check in the validation sweep ("auth" there is short for "author",
    nothing to do with authentication, and there's no redirect involved anyway).
    Requiring the title to *also* describe a login page rules that out, since a
    legitimate art-history page was never going to be titled "Sign in to ...".

    Deliberately does NOT do mid-path matching or inspect return-URL query
    parameters (e.g. "?continue=" / "?returnTo=") - reviewed and rejected: against
    this population, the only additional catch either would bring is a bare
    docs.google.com/spreadsheets/ app link, which would be a false positive.
    """
    page_title = _normalize_apostrophes(page_title)
    if not _redirected(url, final_url) or not final_url:
        return False
    final_path = urlsplit(final_url).path
    if not _LOGIN_PATH_RE.search(final_path):
        return False
    if not page_title:
        return False
    title = page_title.lower()
    return any(phrase in title for phrase in _LOGIN_TITLE_PHRASES)


# 11 alphanumeric/hyphen/underscore characters is YouTube's fixed video-id length,
# in both watch?v= and youtu.be/ URL forms.
YOUTUBE_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

# Hosts serving YouTube's normal player: the plain domain, its privacy-enhanced
# "-nocookie" variant (embeds opted into that mode), and the music.youtube.com
# front end (same video ids, its own domain).
_YOUTUBE_HOSTS = frozenset({"youtube.com", "youtube-nocookie.com", "music.youtube.com"})

# The path prefixes youtube.com (and -nocookie/music variants) serve a single
# video under, other than /watch - an embed iframe src, a Shorts url, the legacy
# /v/ flash-player path, and a live-stream url.
_YOUTUBE_EMBED_PREFIXES = frozenset({"embed", "shorts", "v", "live"})


def youtube_video_id(url: str) -> str | None:
    """The video id from a youtube.com/watch, youtu.be/, or youtube.com/{embed,
    shorts,v,live}/ URL (youtube-nocookie.com and music.youtube.com count too), or
    None for anything else (playlists, channels, other hosts) - those aren't a
    single video the oEmbed endpoint (see checker.check_link) can probe, so they
    fall through to a normal fetch instead.

    /embed/videoseries is excluded explicitly even though "videoseries" happens to
    be exactly 11 characters and id-shaped (see YOUTUBE_VIDEO_ID_RE) - it's
    YouTube's fixed literal path for embedding an entire *playlist*, not a video id
    at all, and nothing about its shape alone would otherwise distinguish it from a
    real id.
    """
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    path = parsed.path.rstrip("/")  # tolerate a trailing slash, e.g. "/watch/"

    if host == "youtu.be":
        video_id = path.strip("/").split("/")[0]
    elif host in _YOUTUBE_HOSTS:
        if path == "/watch":
            values = parse_qs(parsed.query).get("v")
            video_id = values[0] if values else None
        else:
            segments = path.strip("/").split("/")
            if len(segments) != 2 or segments[0] not in _YOUTUBE_EMBED_PREFIXES:
                return None
            if segments[1] == "videoseries":  # playlist embed, not a video id
                return None
            video_id = segments[1]
    else:
        return None
    return video_id if video_id and YOUTUBE_VIDEO_ID_RE.match(video_id) else None


def is_unavailable_video(url: str, http_status: int | None) -> bool:
    """True for a YouTube video url whose oEmbed probe came back 403 (private) or 404
    (deleted) - see checker.classify, where this is checked ahead of the plain
    404/410 handling so a video gets this more specific reason instead.

    Caution: the 403-means-private mapping is validated against live links at this
    project's volume, but YouTube can also 403 an oEmbed request for quota/abuse
    throttling that has nothing to do with the video itself - that would mass-flag
    every video checked while it lasts. Per-domain rate limiting and the slow
    steady-state recheck cadence (see checker.next_state) keep this project's
    request volume far below anything likely to trigger that, but if a spike of
    video_unavailable ever shows up in the reports, this assumption is the first
    thing to revisit.
    """
    return youtube_video_id(url) is not None and http_status in (403, 404)


def detect_rot(
    *,
    url: str,
    final_url: str | None,
    http_status: int | None,
    page_title: str | None = None,
    body_excerpt: str | None = None,
) -> str | None:
    """Run every heuristic against one check's signals and return the first
    matching reason slug, or None if nothing matched. Only ever meaningful for a
    2xx response - a non-2xx (or missing) status returns None immediately, since
    every heuristic here is about a request that "succeeded" but shouldn't have.

    Order: parking, hijacked, soft_404, homepage_redirect, media_replaced,
    auth_wall.
    """
    if http_status is None or not (200 <= http_status < 300):
        return None

    if parking(final_url, page_title, body_excerpt):
        return REASON_PARKING
    if hijacked(page_title):
        return REASON_HIJACKED
    if soft_404(url, final_url, page_title, body_excerpt):
        return REASON_SOFT_404
    if homepage_redirect(url, final_url):
        return REASON_HOMEPAGE_REDIRECT
    if media_replaced(url, page_title):
        return REASON_MEDIA_REPLACED
    if auth_wall(url, final_url, page_title):
        return REASON_AUTH_WALL
    return None
