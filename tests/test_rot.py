from linkcheck import rot

# --- homepage_redirect() ---


def test_homepage_redirect_deep_to_root_cross_host():
    assert rot.homepage_redirect("http://example.com/deep/lesson", "https://otherdomain.com/") is True


def test_homepage_redirect_deep_to_root_same_host():
    assert rot.homepage_redirect("http://example.com/deep/lesson", "http://example.com/") is True


def test_homepage_redirect_no_redirect_at_all():
    assert rot.homepage_redirect("http://example.com/deep/lesson", "http://example.com/deep/lesson") is False


def test_homepage_redirect_none_final_url():
    assert rot.homepage_redirect("http://example.com/deep/lesson", None) is False


def test_homepage_redirect_query_only_original_does_not_trigger():
    # a `?utm=`-stripping redirect from a query-only URL to the bare homepage is
    # normal and fine - deliberately not flagged, trading recall for precision
    assert rot.homepage_redirect("http://example.com/?p=123", "http://example.com/") is False


def test_homepage_redirect_shortener_landing_on_root_does_not_trigger():
    # the shortener's path is an opaque token - landing on some homepage may be
    # exactly what it was configured to do
    assert rot.homepage_redirect("http://bit.ly/abcd", "https://example.com/") is False


def test_homepage_redirect_shortener_www_subdomain_still_exempt():
    # R10: the shortener exemption is keyed on the registrable domain, not the
    # exact host, so a "www." (or other subdomain) spelling of a shortener still
    # counts
    assert rot.homepage_redirect("http://www.tinyurl.com/abcd", "https://example.com/") is False


def test_homepage_redirect_explicit_default_port_is_not_a_redirect():
    # R1: httpx normalizes away an explicit default port (":80") in its response
    # URLs, but the ORIGINAL url as authored/stored is compared raw - without
    # normalizing both sides the same way, this looks like a redirect happened
    # when nothing actually changed
    assert (
        rot.homepage_redirect("http://example.com:80/deep/lesson", "http://example.com/deep/lesson")
        is False
    )


def test_homepage_redirect_deep_to_deep_does_not_trigger():
    assert (
        rot.homepage_redirect("http://example.com/deep/lesson", "http://example.com/other/lesson")
        is False
    )


def test_homepage_redirect_root_to_root_www_canonicalization_does_not_trigger():
    assert rot.homepage_redirect("http://example.com/", "https://www.example.com/") is False


def test_homepage_redirect_deep_to_root_with_query_does_not_trigger():
    assert rot.homepage_redirect("http://example.com/deep/lesson", "http://example.com/?ref=x") is False


def test_homepage_redirect_recognizes_common_homepage_paths():
    for path in ("/index.html", "/index.htm", "/index.php", "/home"):
        assert rot.homepage_redirect("http://example.com/deep/lesson", f"http://example.com{path}") is True


def test_homepage_redirect_recognizes_default_aspx_as_final_homepage():
    # R2: the final-side homepage match is broadened beyond the original fixed set
    # to any of "index"/"default"/"home" plus a short extension, e.g. IIS's
    # "/default.aspx"-style root document
    assert rot.homepage_redirect("http://example.com/deep/lesson", "http://example.com/default.aspx") is True


def test_homepage_redirect_original_home_path_to_root_does_not_trigger():
    # original was already "/home" - redirecting to "/" is canonicalization, not rot
    assert (
        rot.homepage_redirect("http://www.winchestercollege.org/home", "https://www.winchestercollege.org/")
        is False
    )


def test_homepage_redirect_original_index_variant_to_root_does_not_trigger():
    # original was already an "/index.*" homepage spelling
    assert (
        rot.homepage_redirect("http://www.clinique.es/index.tmpl", "https://www.clinique.es/")
        is False
    )


def test_homepage_redirect_index_php_path_info_url_is_now_eligible():
    # R2: this used to be exempted entirely by the old unanchored
    # startswith("/index.") check, since "/index.php/2012/10/08/..." also starts
    # with "/index." even though it's a MediaWiki/Joomla path-info URL for real,
    # specific content - the anchored regex only exempts a BARE "/index.*"
    # homepage spelling, so this now gets a normal homepage_redirect verdict
    assert (
        rot.homepage_redirect(
            "http://example.com/index.php/2012/10/08/how-bird-wings-work/",
            "http://example.com/",
        )
        is True
    )


def test_homepage_redirect_to_dedicated_subdomain_does_not_trigger():
    # content moved to live at its own dedicated subdomain root - a move, not rot
    assert (
        rot.homepage_redirect(
            "http://www.sciencemuseum.org.uk/launchpad/launchball/",
            "https://launchball.sciencemuseum.org.uk/",
        )
        is False
    )


def test_homepage_redirect_to_dedicated_subdomain_does_not_trigger_no_www():
    assert (
        rot.homepage_redirect(
            "http://www.covenantseminary.edu/resources/",
            "https://resources.covenantseminary.edu/",
        )
        is False
    )


def test_homepage_redirect_to_dedicated_subdomain_bare_host():
    assert rot.homepage_redirect("https://archive.org/web/", "https://web.archive.org/") is False


def test_homepage_redirect_subdomain_to_www_root_still_triggers():
    # reverse direction: subdomain content moved OFF to the main www root - still rot
    assert (
        rot.homepage_redirect(
            "https://video.nationalgeographic.com/video/oceans-narrated-by-sylvia-earle/oceans-barrier-reef",
            "https://www.nationalgeographic.com/",
        )
        is True
    )


def test_homepage_redirect_deeper_subdomain_to_shallower_subdomain_still_triggers():
    assert (
        rot.homepage_redirect(
            "https://owl.english.purdue.edu/owl/resource/747/03/",
            "https://owl.purdue.edu/",
        )
        is True
    )


def test_homepage_redirect_sibling_subdomain_still_triggers():
    assert (
        rot.homepage_redirect(
            "https://apstudent.collegeboard.org/apcourse/ap-physics-b/exam-practice",
            "https://apstudents.collegeboard.org/",
        )
        is True
    )


# --- soft_404() ---


_URL = "http://example.com/page"


def test_soft_404_wordpress_default_theme_title():
    assert rot.soft_404(_URL, None, "Page not found – Example Site", None) is True


def test_soft_404_cpanel_account_suspended_title():
    assert rot.soft_404(_URL, None, "Account Suspended", None) is True


def test_soft_404_exact_title_404():
    assert rot.soft_404(_URL, None, "404", None) is True


def test_soft_404_exact_title_not_found():
    assert rot.soft_404(_URL, None, "Not Found", None) is True


def test_soft_404_benign_title_containing_404_as_content_does_not_match():
    # a substring "404" alone isn't enough - only the specific phrases below are
    assert rot.soft_404(_URL, None, "The story of the 404 boys' brigade", None) is False


def test_soft_404_benign_title_unrelated_to_404_does_not_match():
    assert rot.soft_404(_URL, None, "Fahrenheit 451 study guide", None) is False


def test_soft_404_body_phrase_match():
    assert rot.soft_404(_URL, None, None, "the page you are looking for does not exist") is True


def test_soft_404_blogger_deleted_blog_body_phrase():
    assert (
        rot.soft_404(_URL, None, None, "the blog you were looking for does not exist has been deleted")
        is True
    )


def test_soft_404_no_title_or_body_does_not_match():
    assert rot.soft_404(_URL, None, None, None) is False


def test_soft_404_generic_error_mention_in_body_does_not_match():
    # loose/generic body mentions must not match - only the specific, full phrases do
    assert rot.soft_404(_URL, None, None, "an error occurred loading the video, please try again") is False


def test_soft_404_new_title_phrase_page_was_not_found():
    assert rot.soft_404(_URL, None, "This Page Was Not Found - mac-on-campus.com", None) is True


def test_soft_404_curly_apostrophe_title_matches_ascii_phrase():
    # R3: WordPress/Squarespace emit a curly right single quote, not an ASCII
    # apostrophe - "page can’t be found" must match the same as "page can't be found"
    assert rot.soft_404(_URL, None, "Page Can’t Be Found", None) is True


def test_soft_404_new_title_regex_article_not_found():
    # R4: the folded not-found-title regex covers nouns beyond the original
    # fixed-string list (post/article/resource/content/record/entry)
    assert rot.soft_404(_URL, None, "Article Not Found", None) is True


def test_soft_404_new_title_regex_page_could_not_be_found():
    assert rot.soft_404(_URL, None, "Page Could Not Be Found", None) is True


def test_soft_404_new_title_regex_page_couldnt_be_found_curly_apostrophe():
    assert rot.soft_404(_URL, None, "Page Couldn’t Be Found", None) is True


# --- soft_404() server-default title prefixes ---


def test_soft_404_nginx_default_title():
    assert rot.soft_404(_URL, None, "Welcome to nginx!", None) is True


def test_soft_404_apache2_ubuntu_default_title():
    assert rot.soft_404(_URL, None, "Apache2 Ubuntu Default Page: It works", None) is True


def test_soft_404_apache_test_page_title():
    assert rot.soft_404(_URL, None, "Apache HTTP Server Test Page powered by CentOS", None) is True


def test_soft_404_iis_default_title():
    assert rot.soft_404(_URL, None, "IIS Windows Server", None) is True


def test_soft_404_default_web_site_page_title():
    assert rot.soft_404(_URL, None, "Default Web Site Page", None) is True


def test_soft_404_directory_listing_title_is_not_flagged():
    # A bare directory listing can be legitimate, intentional content (biblehub.com/
    # childrens/ serves one on purpose) - too ambiguous to treat as rot on title alone.
    assert rot.soft_404("https://biblehub.com/childrens/", None, "Index of /childrens", None) is False


def test_soft_404_prefix_does_not_match_unrelated_title_mentioning_index():
    assert rot.soft_404(_URL, None, "Index of Contents - Chapter 1", None) is False


# --- soft_404() error-path redirect ---


def test_soft_404_redirect_to_error_path():
    assert rot.soft_404("http://example.com/lesson", "http://example.com/404", None, None) is True


def test_soft_404_redirect_to_error_path_html_extension():
    assert rot.soft_404("http://example.com/lesson", "http://example.com/404.html", None, None) is True


def test_soft_404_redirect_to_error_path_with_trailing_slash():
    assert rot.soft_404("http://example.com/lesson", "http://example.com/page-not-found/", None, None) is True


def test_soft_404_redirect_to_normal_page_does_not_match():
    assert rot.soft_404("http://example.com/lesson", "http://example.com/other-lesson", None, None) is False


def test_soft_404_no_redirect_error_looking_path_does_not_match():
    # the ORIGINAL url itself being "/404" (no redirect at all) isn't this heuristic's case
    assert rot.soft_404("http://example.com/404", "http://example.com/404", None, None) is False


def test_soft_404_redirect_to_error_path_deeper_route():
    # R5: the anchored last-segment regex still catches an error slug at the end
    # of a deeper route, not just a bare top-level one
    assert rot.soft_404("http://example.com/lesson", "http://example.com/en/404", None, None) is True


def test_soft_404_redirect_to_lesson_numbered_404_does_not_match():
    # R5: "404" merely appearing as a substring of an unrelated last segment (a
    # lesson number, not a server routing straight to its own error page) must not
    # match - the segment has to consist ENTIRELY of the error-page slug
    assert (
        rot.soft_404("http://example.com/lesson", "http://example.com/lesson-404-review", None, None)
        is False
    )


# --- parking() ---


def test_parking_sedoparking_final_host():
    assert rot.parking("http://sedoparking.com/some/path", None, None) is True


def test_parking_registrable_domain_match_with_subdomain():
    assert rot.parking("http://www.sedoparking.com/x", None, None) is True


def test_parking_exact_host_parked_porkbun():
    assert rot.parking("http://parked.porkbun.com/", None, None) is True


def test_parking_porkbun_main_site_is_not_flagged():
    # porkbun.com itself is a real registrar - only the parked-subdomain host counts
    assert rot.parking("https://porkbun.com/", None, None) is False


def test_parking_godaddy_courtesy_body_phrase():
    assert rot.parking(None, None, "parked free, courtesy of godaddy") is True


def test_parking_domain_for_sale_body_phrase():
    assert rot.parking(None, "example.com", "this domain is for sale - buy this domain today") is True


def test_parking_domain_is_for_sale_bare_regex_match():
    # R8: the folded regex catches phrasing beyond the original four fixed strings,
    # e.g. "domain is for sale" without a leading "this"/"the"/"that"
    assert rot.parking(None, None, "domain is for sale, contact us for pricing") is True


def test_parking_might_be_for_sale_body_phrase():
    assert rot.parking(None, None, "this domain might be for sale") is True


def test_parking_benign_for_sale_body_does_not_match():
    # bare "for sale" is excluded - real content (a store, a marketplace) sells things
    assert rot.parking(None, None, "this book is for sale at our store for $12") is False


def test_parking_title_and_body_seam_does_not_match_across_the_boundary():
    # R12: title and body are tested separately now - a phrase split across the
    # title/body seam must not read as one continuous match
    assert rot.parking(None, "buy this", "domain today, act now") is False


def test_parking_any_registrar_parked_label_host():
    # R8: PARKING_EXACT_HOSTS is generalized to any host whose leftmost label is
    # exactly "parked", not just the one hardcoded parked.porkbun.com example
    assert rot.parking("http://parked.namecheap.com/", None, None) is True


def test_parking_university_visitor_parking_subdomain_does_not_match():
    # R8: a "parking." (not "parked.") subdomain label is a real link in this
    # population (university visitor parking info) and must never be caught
    assert rot.parking("http://parking.example.edu/", None, None) is False


def test_parking_related_searches_alone_does_not_match():
    assert rot.parking(None, None, "related searches: math worksheets, algebra help") is False


def test_parking_no_signals_does_not_match():
    assert rot.parking("https://example.com/lesson", "Lesson 1", "welcome to the lesson") is False


def test_parking_expireddomains_final_host():
    assert (
        rot.parking(
            "https://expireddomains.com/domain/proofsfromthebook.com",
            "Buy proofsfromthebook.com – Premium Expired .com Domain on GoDaddy | ExpiredDomains.com",
            None,
        )
        is True
    )


# --- hijacked() ---


def test_hijacked_gambling_spam_title():
    assert (
        rot.hijacked("SLOT GACOR ✈️ Situs Slot88 Online Paling Resmi & Mudah Maxwin No.1")
        is True
    )


def test_hijacked_pharma_token_title():
    assert rot.hijacked("Cheap Viagra and Cialis Online - Best Prices") is True


def test_hijacked_bare_gacor_title():
    # R7: "slot gacor" was dropped as its own entry - bare "gacor" alone (without
    # a preceding "slot") must still match
    assert rot.hijacked("Situs Gacor Hari Ini Terpercaya 2026") is True


def test_hijacked_terpercaya_title():
    assert rot.hijacked("Bandar Terpercaya No.1 Indonesia") is True


def test_hijacked_legit_title_with_specialist_substring_does_not_match():
    # "cialis" is a bare substring of "specialist" - \b anchoring must rule this out
    assert rot.hijacked("Jobs in Antarctica with the Australian Antarctic Program - Specialist Roles") is False


def test_hijacked_legit_title_communism_synonym_does_not_match():
    assert rot.hijacked("The Basic Understanding of Communism - Synonym") is False


def test_hijacked_no_title_does_not_match():
    assert rot.hijacked(None) is False


# --- media_replaced() ---


def test_media_replaced_pdf_titled_after_bare_domain():
    assert (
        rot.media_replaced(
            "http://jimmiescollage.com/downloads/writing/peer-editing.pdf",
            "jimmiescollage.com",
        )
        is True
    )


def test_media_replaced_pdf_titled_after_unrelated_site_tagline():
    assert (
        rot.media_replaced(
            "http://www.saylor.org/site/wp-content/uploads/2010/11/The-Rights-of-the-Colonists.pdf",
            "Saylor University — Where Ambition Meets Excellence",
        )
        is True
    )


def test_media_replaced_kiddle_image_page_names_the_file_does_not_match():
    assert (
        rot.media_replaced(
            "https://kids.kiddle.co/Image:Lange-MigrantMother02.jpg",
            "Image: Lange-MigrantMother02",
        )
        is False
    )


def test_media_replaced_archive_org_numeric_mp3_stem_does_not_match():
    # the filename stem is purely numeric ("28733-10") - never counts as significant,
    # so there's nothing to compare against the title and this must not flag
    assert (
        rot.media_replaced(
            "https://archive.org/details/theadventuresofs28733gut/mp3/28733-10.mp3",
            "The Adventures of Sherlock Holmes : Arthur Conan Doyle : Free Download, Borrow, ...",
        )
        is False
    )


def test_media_replaced_wikipedia_file_page_names_the_file_does_not_match():
    assert (
        rot.media_replaced(
            "https://en.wikipedia.org/wiki/File:SantaCruz-CuevaManos-P2210651b.jpg",
            "File:SantaCruz-CuevaManos-P2210651b.jpg - Wikipedia",
        )
        is False
    )


def test_media_replaced_no_title_does_not_match():
    assert rot.media_replaced("http://example.com/file.pdf", None) is False


def test_media_replaced_svg_extension():
    # R9: media extensions were extended to include .svg
    assert rot.media_replaced("http://example.com/icons/rocket-launch.svg", "Unrelated Title") is True


def test_media_replaced_ogg_extension():
    # R9: media extensions were extended to include .ogg
    assert rot.media_replaced("http://example.com/audio/lecture-one.ogg", "Unrelated Title") is True


def test_media_replaced_svg_wikipedia_file_page_names_the_file_does_not_match():
    # the Wikipedia-File:-page guard applies identically to the newly added
    # extensions - a "File:Foo.svg" page still names the file in its own title
    assert (
        rot.media_replaced(
            "https://en.wikipedia.org/wiki/File:Example-diagram.svg",
            "File:Example-diagram.svg - Wikipedia",
        )
        is False
    )


def test_media_replaced_archive_org_details_avi_subfile_does_not_match():
    # R9 follow-up: the offline sweep found archive.org "/details/<item>/<sub-file>"
    # pages (a live viewer for one file within a multi-file item) are titled after
    # the item as a whole - the sub-file's own name (here "William+Bradford.avi")
    # legitimately never appears in it, and that's true regardless of word overlap,
    # not something the pure-digit-stem exemption above happens to catch
    assert (
        rot.media_replaced(
            "https://archive.org/details/AnimatedHeroClassics/William+Bradford.avi",
            "Animated Hero Classics : Free Download, Borrow, and Streaming : Internet Archive",
        )
        is False
    )


def test_media_replaced_non_archive_org_avi_with_disjoint_title_still_matches():
    # the archive.org "/details/" exemption must not swallow a genuine
    # media_replaced hit elsewhere - a plain hosting migration serving an unrelated
    # HTML page at an old .avi url still flags
    assert (
        rot.media_replaced(
            "http://example.com/videos/great-migration.avi",
            "Unrelated Title",
        )
        is True
    )


def test_media_replaced_non_media_extension_does_not_match():
    assert rot.media_replaced("http://example.com/lesson.html", "Unrelated Title") is False


# --- auth_wall() ---


def test_auth_wall_timetoast_sign_in_redirect():
    assert (
        rot.auth_wall(
            "http://www.timetoast.com/timelines",
            "https://www.timetoast.com/users/sign_in",
            "Sign in to Timetoast | Timetoast",
        )
        is True
    )


def test_auth_wall_collegeboard_login_redirect():
    assert (
        rot.auth_wall(
            "https://bigfuture.collegeboard.org/pay-for-college/college-costs/true-cost-of-attendance",
            "https://account.collegeboard.org/login/login?a=b",
            "Account Login",
        )
        is True
    )


def test_auth_wall_ibiblio_author_path_no_redirect_does_not_match():
    # "auth" here is short for "author" - no redirect at all, so the path condition
    # never even gets checked; this is the FP mode requiring BOTH conditions guards against
    assert (
        rot.auth_wall(
            "http://www.ibiblio.org/wm/paint/auth/grunewald/",
            None,
            "WebMuseum: Grünewald, Matthias",
        )
        is False
    )


def test_auth_wall_login_path_without_login_title_does_not_match():
    assert (
        rot.auth_wall(
            "http://example.com/account",
            "http://example.com/login",
            "Welcome Back",
        )
        is False
    )


def test_auth_wall_login_title_without_login_path_does_not_match():
    assert (
        rot.auth_wall(
            "http://example.com/account",
            "http://example.com/dashboard",
            "Please sign in to continue",
        )
        is False
    )


def test_auth_wall_wp_login_php_path_matches():
    # R6: the simplified regex newly catches "/wp-login.php" (an arbitrary prefix
    # glued onto the final "login" segment, plus a file extension)
    assert (
        rot.auth_wall(
            "http://example.com/wp-admin",
            "http://example.com/wp-login.php",
            "Log In ‹ Example — WordPress",
        )
        is True
    )


def test_auth_wall_servicelogin_path_matches():
    # R6: the simplified regex newly catches capitalized "ServiceLogin"-style paths
    assert (
        rot.auth_wall(
            "http://example.com/docs",
            "https://accounts.google.com/ServiceLogin",
            "Sign in - Google Accounts",
        )
        is True
    )


def test_auth_wall_plugin_path_does_not_match():
    # R6: "plugin" ends in "-in" but not "-login"/"-signin" - must not match
    assert (
        rot.auth_wall(
            "http://example.com/x",
            "http://example.com/some-plugin",
            "Log In",
        )
        is False
    )


def test_auth_wall_checkin_path_does_not_match():
    assert (
        rot.auth_wall(
            "http://example.com/x",
            "http://example.com/guest-checkin",
            "Sign In",
        )
        is False
    )


def test_auth_wall_design_path_does_not_match():
    assert (
        rot.auth_wall(
            "http://example.com/x",
            "http://example.com/graphic-design",
            "Login",
        )
        is False
    )


def test_auth_wall_signing_an_apartment_lease_path_does_not_match():
    assert (
        rot.auth_wall(
            "http://example.com/x",
            "http://example.com/signing-an-apartment-lease",
            "Sign In",
        )
        is False
    )


# --- youtube_video_id() / is_unavailable_video() ---


def test_youtube_video_id_watch_url():
    assert rot.youtube_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_youtube_video_id_short_url():
    assert rot.youtube_video_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_youtube_video_id_v_not_first_query_param():
    assert (
        rot.youtube_video_id("https://www.youtube.com/watch?list=PL123&v=dQw4w9WgXcQ&index=2")
        == "dQw4w9WgXcQ"
    )


def test_youtube_video_id_playlist_url_returns_none():
    assert rot.youtube_video_id("https://www.youtube.com/playlist?list=PL123") is None


def test_youtube_video_id_channel_url_returns_none():
    assert rot.youtube_video_id("https://www.youtube.com/channel/UC123") is None


def test_youtube_video_id_other_host_returns_none():
    assert rot.youtube_video_id("https://vimeo.com/12345") is None


def test_youtube_video_id_embed_url():
    assert rot.youtube_video_id("https://www.youtube.com/embed/dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_youtube_video_id_shorts_url():
    assert rot.youtube_video_id("https://www.youtube.com/shorts/dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_youtube_video_id_legacy_v_url():
    assert rot.youtube_video_id("https://www.youtube.com/v/dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_youtube_video_id_live_url():
    assert rot.youtube_video_id("https://www.youtube.com/live/dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_youtube_video_id_nocookie_embed_url():
    assert rot.youtube_video_id("https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_youtube_video_id_music_youtube_watch_url():
    assert rot.youtube_video_id("https://music.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_youtube_video_id_watch_url_with_trailing_slash():
    assert rot.youtube_video_id("https://www.youtube.com/watch/?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_youtube_video_id_embed_videoseries_returns_none():
    # CRITICAL guard: "videoseries" is exactly 11 chars and id-shaped, but
    # /embed/videoseries is YouTube's fixed path for embedding an entire
    # playlist, not a single video - must return None
    assert rot.youtube_video_id("https://www.youtube.com/embed/videoseries?list=PL123") is None


def test_is_unavailable_video_403_is_unavailable():
    assert rot.is_unavailable_video("https://www.youtube.com/watch?v=dQw4w9WgXcQ", 403) is True


def test_is_unavailable_video_404_is_unavailable():
    assert rot.is_unavailable_video("https://youtu.be/dQw4w9WgXcQ", 404) is True


def test_is_unavailable_video_401_is_not_unavailable():
    # embedding-disabled, not deletion/privacy - the video is still watchable
    assert rot.is_unavailable_video("https://www.youtube.com/watch?v=dQw4w9WgXcQ", 401) is False


def test_is_unavailable_video_non_youtube_url_is_never_unavailable():
    assert rot.is_unavailable_video("https://vimeo.com/12345", 404) is False


# --- detect_rot() composition ---


def test_detect_rot_returns_none_for_non_2xx_status():
    assert (
        rot.detect_rot(
            url="http://example.com/deep",
            final_url="http://example.com/",
            http_status=404,
        )
        is None
    )


def test_detect_rot_returns_none_for_missing_status():
    assert rot.detect_rot(url="http://example.com/deep", final_url=None, http_status=None) is None


def test_detect_rot_returns_parking_reason():
    assert (
        rot.detect_rot(
            url="http://example.com/deep",
            final_url="http://sedoparking.com/x",
            http_status=200,
        )
        == "parking"
    )


def test_detect_rot_returns_soft_404_reason():
    assert (
        rot.detect_rot(
            url="http://example.com/deep",
            final_url="http://example.com/deep",
            http_status=200,
            page_title="404",
        )
        == "soft_404"
    )


def test_detect_rot_returns_homepage_redirect_reason():
    assert (
        rot.detect_rot(
            url="http://example.com/deep/lesson",
            final_url="http://example.com/",
            http_status=200,
        )
        == "homepage_redirect"
    )


def test_detect_rot_returns_none_when_nothing_matches():
    assert (
        rot.detect_rot(
            url="http://example.com/deep/lesson",
            final_url="http://example.com/deep/lesson",
            http_status=200,
            page_title="A Great Lesson",
            body_excerpt="welcome to today's lesson on fractions",
        )
        is None
    )


def test_detect_rot_parking_takes_precedence_over_homepage_redirect():
    # a redirect to a parked domain's bare root matches both parking (host) and
    # homepage_redirect - parking is checked first and wins
    assert (
        rot.detect_rot(
            url="http://example.com/deep/lesson",
            final_url="http://sedoparking.com/",
            http_status=200,
        )
        == "parking"
    )


def test_detect_rot_returns_hijacked_reason():
    assert (
        rot.detect_rot(
            url="http://example.com/deep",
            final_url="http://example.com/deep",
            http_status=200,
            page_title="SLOT GACOR Situs Slot88 Online Paling Resmi & Mudah Maxwin No.1",
        )
        == "hijacked"
    )


def test_detect_rot_returns_media_replaced_reason():
    assert (
        rot.detect_rot(
            url="http://jimmiescollage.com/downloads/writing/peer-editing.pdf",
            final_url="http://jimmiescollage.com/downloads/writing/peer-editing.pdf",
            http_status=200,
            page_title="jimmiescollage.com",
        )
        == "media_replaced"
    )


def test_detect_rot_returns_auth_wall_reason():
    assert (
        rot.detect_rot(
            url="http://www.timetoast.com/timelines",
            final_url="https://www.timetoast.com/users/sign_in",
            http_status=200,
            page_title="Sign in to Timetoast | Timetoast",
        )
        == "auth_wall"
    )
