"""How the system decides what a page offers. The first version labelled a charity's information article "CAPTCHA required" because
the word appeared somewhere in the page's code; nothing could be submitted there at all."""
from src.backlinks import qualify

FORM = ('<form action="/submit" method="post"><input name="name"><input type="email" name="email"><textarea name="message"></textarea>'
        '<input type="submit"></form>')
SCRIPT = '<script src="https://www.google.com/recaptcha/api.js" async></script>'


def mechanism(html):
    return qualify.inspect_page(html, "https://x.example.org/p")["mechanism"]


def test_an_information_page_that_merely_mentions_a_captcha_has_no_submission_route():
    page = ("<html><body><h1>Coping with phobias</h1><p>Treatment and self-care ideas.</p>"
            "<script>window.consent = {captcha: 'recaptcha', provider: 'turnstile'};</script></body></html>")
    assert mechanism(page) == "no_submission_route_found"


def test_a_newsletter_box_with_a_captcha_script_is_not_a_submission_form():
    page = f'<html><body><p>Our guides</p><form><input type="email" name="e"><input type="submit"></form>{SCRIPT}</body></html>'
    assert mechanism(page) == "no_submission_route_found"


def test_a_real_form_with_a_captcha_widget_is_reported_as_one():
    assert mechanism(f'<html><body><h1>Write for us</h1>{FORM.replace("</form>", "<div class=\'g-recaptcha\' data-sitekey=\'k\'></div></form>")}</body></html>') == "captcha_required"
    assert mechanism(f"<html><body><h1>Write for us</h1>{FORM}{SCRIPT}</body></html>") == "captcha_required"
    assert mechanism('<html><body><h1>Write for us</h1><form><input name="a"><input name="b"><textarea></textarea></form><iframe src="https://hcaptcha.com/x"></iframe></body></html>') == "captcha_required"


def test_a_real_form_without_a_captcha_is_a_plain_form():
    assert mechanism(f"<html><body><h1>Write for us</h1>{FORM}</body></html>") == "form_submission"


def test_a_login_box_in_the_header_does_not_make_an_information_page_need_an_account():
    page = '<html><body><form><input type="text" name="u"><input type="password" name="p"></form><h1>Sleep tips</h1><p>Read our advice.</p></body></html>'
    assert mechanism(page) == "no_submission_route_found"


def test_a_page_that_says_to_log_in_to_submit_needs_an_account():
    assert mechanism("<html><body><h1>Contribute</h1><p>You need to log in to submit an article.</p></body></html>") == "account_required"
    assert mechanism('<html><body><h1>Guest posts: pitch us</h1><p>Please contribute.</p><form><input type="password" name="p"></form></body></html>') == "account_required"


def test_submissions_by_email_are_recognised_only_with_pitch_wording():
    assert mechanism('<html><body><h1>Write for us</h1><a href="mailto:editor@example.org">Email the editor</a></body></html>') == "email_only"
    assert mechanism('<html><body><h1>About us</h1><a href="mailto:info@example.org">info@example.org</a></body></html>') == "no_submission_route_found"
