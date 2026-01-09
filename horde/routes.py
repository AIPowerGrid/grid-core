# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import os
import json
import random
import secrets
from uuid import uuid4

import oauthlib
import requests
from flask import redirect, render_template, request, send_from_directory, url_for
from flask_dance.contrib.discord import discord
from flask_dance.contrib.github import github
from flask_dance.contrib.google import google
from markdown import markdown

from horde import vars as hv
from horde.argparser import maintenance
from horde.classes.base import settings
from horde.classes.base.news import News
from horde.classes.base.user import User
from horde.consts import HORDE_API_VERSION, HORDE_VERSION
from horde.countermeasures import CounterMeasures
from horde.database import functions as database
from horde.flask import HORDE, cache, db
from horde.logger import logger
from horde.patreon import patrons
from horde.utils import ConvertAmount, hash_api_key, is_profane, sanitize_string
from horde.vars import (
    google_verification_string,
    horde_contact_email,
    horde_logo,
    horde_repository,
    horde_title,
    horde_url,
    img_url,
)

dance_return_to = "/"


@logger.catch(reraise=True)
@HORDE.route("/")
# @cache.cached(timeout=300)
def index():
    with open(os.getenv("HORDE_MARKDOWN_INDEX", "index_stable.md")) as index_file:
        index = index_file.read()
    align_image = 0
    big_image = align_image
    while big_image == align_image:
        big_image = random.randint(1, 5)
    policies = """
## Policies

<div style="text-align: center;">

<a href="/privacy">Privacy Policy</a>

<a href="/terms">Terms of Service</a>

</div>"""
    news = ""
    sorted_news = News().sorted_news()
    for riter in range(len(sorted_news)):
        news += f"* {sorted_news[riter]['newspiece']}\n"
        if riter > 1:
            break
    totals = database.get_total_usage()
    processing_totals = database.retrieve_totals()
    (
        interrogation_worker_count,
        interrogation_worker_thread_count,
    ) = database.count_active_workers("interrogation")
    image_worker_count, image_worker_thread_count = database.count_active_workers("image")
    text_worker_count, text_worker_thread_count = database.count_active_workers("text")
    # Calculate average performance - use reasonable estimates when no workers are active
    avg_performance = (
        ConvertAmount(database.get_request_avg() * image_worker_thread_count) if image_worker_thread_count > 0 else ConvertAmount(5.0)
    )
    avg_text_performance = (
        ConvertAmount(database.get_request_avg("text") * text_worker_thread_count) if text_worker_thread_count > 0 else ConvertAmount(50.0)
    )
    # We multiple with the divisor again, to get the raw amount, which we can convert to prefix accurately
    total_image_things = ConvertAmount(totals[hv.thing_names["image"]] * hv.thing_divisors["image"])
    total_text_things = ConvertAmount(totals[hv.thing_names["text"]] * hv.thing_divisors["text"])
    queued_image_things = ConvertAmount(
        processing_totals[f"queued_{hv.thing_names['image']}"] * hv.thing_divisors["image"],
    )
    queued_text_things = ConvertAmount(
        processing_totals[f"queued_{hv.thing_names['text']}"] * hv.thing_divisors["text"],
    )
    total_image_fulfillments = ConvertAmount(totals["image_fulfilments"])
    total_text_fulfillments = ConvertAmount(totals["text_fulfilments"])
    total_forms = ConvertAmount(totals["forms"])
    total_threads = image_worker_thread_count + text_worker_thread_count + interrogation_worker_thread_count
    total_workers = image_worker_count + text_worker_count

    # Get logo from environment
    logo_url = os.getenv("HORDE_LOGO", "https://aipowergrid.io/aipg-main.png")

    # Get available models (with error handling)
    try:
        available_models = database.get_available_models()
        image_models = [model for model in available_models if model["type"] == "image"]
        text_models = [model for model in available_models if model["type"] == "text"]

        # Create model lists as HTML
        image_models_list = "<ul style='list-style: none; padding: 0; margin: 0;'>\n"
        for model in image_models[:10]:  # Show top 10
            image_models_list += f"<li style='padding: 4px 0; border-bottom: 1px solid rgba(255,255,255,0.1);'><strong>{model['name']}</strong> <span style='color: #8b949e;'>({model['count']} workers)</span></li>\n"
        if len(image_models) > 10:
            image_models_list += (
                f"<li style='padding: 4px 0; color: #8b949e; font-style: italic;'>... and {len(image_models) - 10} more models</li>\n"
            )
        image_models_list += "</ul>"
        if not image_models:
            image_models_list = "<p style='color: #8b949e;'>No image models currently available</p>"

        text_models_list = "<ul style='list-style: none; padding: 0; margin: 0;'>\n"
        for model in text_models[:10]:  # Show top 10
            text_models_list += f"<li style='padding: 4px 0; border-bottom: 1px solid rgba(255,255,255,0.1);'><strong>{model['name']}</strong> <span style='color: #8b949e;'>({model['count']} workers)</span></li>\n"
        if len(text_models) > 10:
            text_models_list += (
                f"<li style='padding: 4px 0; color: #8b949e; font-style: italic;'>... and {len(text_models) - 10} more models</li>\n"
            )
        text_models_list += "</ul>"
        if not text_models:
            text_models_list = "<p style='color: #8b949e;'>No text models currently available</p>"

        # Get top models by worker count
        top_models = sorted(available_models, key=lambda x: x.get("count", 0), reverse=True)[:5]
        top_models_list = "<ul style='list-style: none; padding: 0; margin: 0;'>\n"
        for model in top_models:
            top_models_list += f"<li style='padding: 4px 0; border-bottom: 1px solid rgba(255,255,255,0.1);'><strong>{model['name']}</strong> <span style='color: #8b949e;'>({model['count']} workers)</span></li>\n"
        top_models_list += "</ul>"
        if not top_models:
            top_models_list = "<p style='color: #8b949e;'>No models currently available</p>"
    except Exception as e:
        # Fallback if models can't be loaded
        logger.warning(f"Failed to load models for index page: {e}")
        image_models_list = "<p style='color: #f85149;'>Unable to load models</p>"
        text_models_list = "<p style='color: #f85149;'>Unable to load models</p>"
        top_models_list = "<p style='color: #f85149;'>Unable to load models</p>"
    image_models = []
    text_models = []

    findex = index.format(
        page_title=horde_title,
        horde_img_url=img_url,
        horde_image=align_image,
        avg_performance=avg_performance.amount,
        avg_thing_name=avg_performance.prefix + hv.raw_thing_names["image"],
        avg_text_performance=avg_text_performance.amount,
        avg_text_thing_name=avg_text_performance.prefix + hv.raw_thing_names["text"],
        total_image_things=total_image_things.amount,
        total_total_image_things_name=total_image_things.prefix + hv.raw_thing_names["image"],
        total_text_things=total_text_things.amount,
        total_text_things_name=total_text_things.prefix + hv.raw_thing_names["text"],
        total_image_fulfillments=total_image_fulfillments.amount,
        total_image_fulfillments_char=total_image_fulfillments.char,
        total_text_fulfillments=total_text_fulfillments.amount,
        total_text_fulfillments_char=total_text_fulfillments.char,
        total_forms=total_forms.amount,
        total_forms_char=total_forms.char,
        image_workers=image_worker_count,
        image_worker_threads=image_worker_thread_count,
        text_workers=text_worker_count,
        text_worker_threads=text_worker_thread_count,
        interrogation_workers=interrogation_worker_count,
        interrogation_worker_threads=interrogation_worker_thread_count,
        total_threads=total_threads,
        total_workers=total_workers,
        logo_url=logo_url,
        total_image_queue=processing_totals["queued_requests"],
        total_text_queue=processing_totals["queued_text_requests"],
        total_forms_queue=processing_totals.get("queued_forms", 0),
        queued_image_things=queued_image_things.amount,
        queued_image_things_name=queued_image_things.prefix + hv.raw_thing_names["image"],
        queued_text_things=queued_text_things.amount,
        queued_text_things_name=queued_text_things.prefix + hv.raw_thing_names["text"],
        maintenance_mode=maintenance.active,
        news=news,
        image_models_count=len(image_models),
        text_models_count=len(text_models),
        image_models_list=image_models_list,
        text_models_list=text_models_list,
        top_models_list=top_models_list,
    )

    style = """<style>
        body {
            background: linear-gradient(135deg, #0d1117 0%, #161b22 100%);
            color: #c9d1d9;
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Noto Sans', Helvetica, Arial, sans-serif;
            line-height: 1.7;
            margin: 0;
            padding: 40px 20px;
            max-width: 1000px;
            margin: 0 auto;
            min-height: 100vh;
        }
        
        h1 {
            color: #f0f6fc;
            font-weight: 700;
            font-size: 2.5rem;
            margin: 0 0 1rem 0;
            border: none;
            text-align: center;
            background: linear-gradient(135deg, #58a6ff, #8b5cf6);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }
        
        h2 {
            color: #f0f6fc;
            font-weight: 600;
            font-size: 1.8rem;
            margin: 0.5rem 0 1rem 0;
            border-bottom: none;
            padding-bottom: 0.5rem;
            position: relative;
            text-align: center;
        }
        
        h3 {
            color: #f0f6fc;
            font-weight: 600;
            font-size: 1.3rem;
            margin: 2rem 0 1rem 0;
            border-bottom: none;
            padding-bottom: 0.5rem;
            text-align: center;
        }
        
        h4, h5, h6 {
            color: #f0f6fc;
            font-weight: 600;
            margin: 1.5rem 0 0.5rem 0;
            text-align: center;
        }
        
        p {
            margin: 1rem 0;
            color: #c9d1d9;
        }
        
        a {
            color: #58a6ff;
            text-decoration: none;
            transition: all 0.2s ease;
            border-bottom: 1px solid transparent;
        }
        
        a:hover {
            color: #8b5cf6;
            border-bottom: 1px solid #8b5cf6;
            text-decoration: none;
        }
        
        code {
            background: rgba(22, 27, 34, 0.8);
            border: 1px solid #30363d;
            border-radius: 6px;
            padding: 3px 8px;
            font-family: 'SF Mono', Monaco, 'Cascadia Code', 'Roboto Mono', Consolas, 'Courier New', monospace;
            color: #c9d1d9;
            font-size: 0.9em;
            backdrop-filter: blur(10px);
        }
        
        pre {
            background: rgba(22, 27, 34, 0.9);
            border: 1px solid #30363d;
            border-radius: 12px;
            padding: 24px;
            overflow-x: auto;
            font-family: 'SF Mono', Monaco, 'Cascadia Code', 'Roboto Mono', Consolas, 'Courier New', monospace;
            margin: 1.5rem 0;
            box-shadow: 0 4px 20px rgba(0, 0, 0, 0.3);
            backdrop-filter: blur(10px);
        }
        
        pre code {
            background: none;
            border: none;
            padding: 0;
            color: #c9d1d9;
        }
        
        hr {
            border: none;
            border-top: 1px solid #30363d;
            margin: 3rem 0;
            position: relative;
        }
        
        hr::after {
            content: '';
            position: absolute;
            top: -1px;
            left: 50%;
            transform: translateX(-50%);
            width: 100px;
            height: 1px;
            background: linear-gradient(90deg, transparent, #58a6ff, transparent);
        }
        
        ul, ol {
            padding-left: 1.5rem;
            margin: 1rem 0;
        }
        
        li {
            margin: 0.5rem 0;
            color: #c9d1d9;
        }
        
        strong {
            color: #f0f6fc;
            font-weight: 600;
        }
        
        blockquote {
            background: rgba(22, 27, 34, 0.5);
            border-left: 4px solid #58a6ff;
            margin: 1.5rem 0;
            padding: 1rem 1.5rem;
            border-radius: 0 8px 8px 0;
            font-style: italic;
            color: #8b949e;
        }
        
        .highlight {
            background: linear-gradient(135deg, rgba(88, 166, 255, 0.1), rgba(139, 92, 246, 0.1));
            border: 1px solid rgba(88, 166, 255, 0.2);
            border-radius: 8px;
            padding: 1.5rem;
            margin: 1.5rem 0;
        }
        
        .card {
            background: rgba(22, 27, 34, 0.6);
            border: 1px solid #30363d;
            border-radius: 12px;
            padding: 1.5rem;
            margin: 1.5rem 0;
            transition: all 0.3s ease;
            backdrop-filter: blur(10px);
        }
        
        .card:hover {
            border-color: #58a6ff;
            box-shadow: 0 8px 32px rgba(88, 166, 255, 0.1);
            transform: translateY(-2px);
        }
        
        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 1.5rem;
            margin: 2rem 0;
        }
        
        .grid-2 {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 1.5rem;
            margin: 2rem 0;
        }
        
        .stat {
            background: rgba(22, 27, 34, 0.7);
            border: 1px solid #30363d;
            border-radius: 12px;
            padding: 1.5rem;
            text-align: center;
            transition: all 0.3s ease;
            backdrop-filter: blur(10px);
        }
        
        .stat:hover {
            border-color: #8b5cf6;
            transform: translateY(-2px);
        }
        
        .stat-value {
            font-size: 1.5rem;
            font-weight: 700;
            color: #58a6ff;
            margin-bottom: 0.5rem;
        }
        
        .stat-label {
            color: #8b949e;
            font-size: 0.9rem;
        }
        
        details {
            margin: 1rem 0;
            border: 1px solid #30363d;
            border-radius: 8px;
            background: rgba(22, 27, 34, 0.5);
        }
        
        details summary {
            padding: 1rem;
            cursor: pointer;
            font-weight: 600;
            color: #f0f6fc;
            background: rgba(22, 27, 34, 0.8);
            border-radius: 8px 8px 0 0;
            transition: background-color 0.2s ease;
        }
        
        details summary:hover {
            background: rgba(22, 27, 34, 0.9);
        }
        
        details[open] summary {
            border-bottom: 1px solid #30363d;
            border-radius: 8px 8px 0 0;
        }
        
        .model-list {
            padding: 1rem;
            font-family: 'SF Mono', Monaco, 'Cascadia Code', 'Roboto Mono', Consolas, 'Courier New', monospace;
            font-size: 0.9rem;
            line-height: 1.6;
            color: #c9d1d9;
            background: rgba(22, 27, 34, 0.3);
            border-radius: 0 0 8px 8px;
        }
        
        .model-list ul {
            margin: 0;
            padding-left: 1rem;
        }
        
        .model-list li {
            margin: 0.3rem 0;
            padding: 0.2rem 0;
        }
        
        @media (max-width: 768px) {
            body {
                padding: 20px 15px;
            }
            
            h1 {
                font-size: 2rem;
            }
            
            h2 {
                font-size: 1.5rem;
            }
            
            .grid {
                grid-template-columns: 1fr;
            }
            
            .grid-2 {
                grid-template-columns: 1fr;
            }
            
            details summary {
                padding: 0.8rem;
                font-size: 0.9rem;
            }
            
                        .model-list {
                padding: 0.8rem;
                font-size: 0.8rem;
            }
        }
        
        .logo-section {
            text-align: center;
            margin-bottom: 2rem;
        }
        
        .logo {
            max-width: 100px;
            height: auto;
            margin-bottom: 0.5rem;
        }
        </style>
    """

    head = f"""<head>
    <title>{horde_title}</title>
    <meta name="google-site-verification" content="{google_verification_string}" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    {style}
    </head>
    """
    return head + markdown(findex + policies)


@HORDE.route("/sponsors")
@logger.catch(reraise=True)
@cache.cached(timeout=300)
def patrons_route():
    all_patrons = ", ".join(patrons.get_names(min_entitlement=3, max_entitlement=99))
    return render_template(
        "document.html",
        doc="sponsors.html",
        page_title="Sponsors",
        all_patrons=all_patrons,
        all_sponsors=patrons.get_sponsors(),
    )


@logger.catch(reraise=True)
def get_oauth_id():
    google_data = None
    discord_data = None
    github_data = None
    patreon_data = None
    authorized = False
    if google.authorized:
        google_user_info_endpoint = "/oauth2/v2/userinfo"
        try:
            google_data = google.get(google_user_info_endpoint).json()
            authorized = True
        except oauthlib.oauth2.rfc6749.errors.TokenExpiredError:
            pass
    if not authorized and discord.authorized:
        discord_info_endpoint = "/api/users/@me"
        try:
            discord_data = discord.get(discord_info_endpoint).json()
            authorized = True
        except oauthlib.oauth2.rfc6749.errors.TokenExpiredError:
            pass
    if not authorized and github.authorized:
        github_info_endpoint = "/user"
        try:
            github_data = github.get(github_info_endpoint).json()
            authorized = True
        except oauthlib.oauth2.rfc6749.errors.TokenExpiredError:
            pass
    # if not authorized and patreon.OAuth(os.getenv("PATREON_CLIENT_ID"), os.getenv("PATREON_CLIENT_SECRET")):
    #     patreon_info_endpoint = '/api/oauth2/token'
    #     try:
    #         patreon_data = github.get(patreon_info_endpoint).json()
    #         authorized = True
    #     except oauthlib.oauth2.rfc6749.errors.TokenExpiredError:
    #         pass
    oauth_id = None
    if google_data:
        oauth_id = f'g_{google_data["id"]}'
    elif discord_data:
        oauth_id = f'd_{discord_data["id"]}'
    elif github_data:
        oauth_id = f'gh_{github_data["id"]}'
    elif patreon_data:
        oauth_id = f'p_{patreon_data["id"]}'
    return oauth_id


@logger.catch(reraise=True)
@HORDE.route("/register", methods=["GET", "POST"])
def register():
    api_key = None
    user = None
    welcome = "Welcome"
    username = ""
    pseudonymous = False
    oauth_id = get_oauth_id()
    if oauth_id:
        user = database.find_user_by_oauth_id(oauth_id)
        if user:
            username = user.username
    use_recaptcha = True
    secret_key = os.getenv("RECAPTCHA_SECRET_KEY")
    if not secret_key:
        use_recaptcha = False
    if request.method == "POST":
        if use_recaptcha:
            try:
                recaptcha_response = request.form["g-recaptcha-response"]
                payload = {"response": recaptcha_response, "secret": secret_key}
                response = requests.post("https://www.google.com/recaptcha/api/siteverify", payload)
                if not response.ok or not response.json()["success"]:
                    return render_template(
                        "recaptcha_error.html",
                        page_title="Recaptcha validation Error!",
                        use_recaptcha=False,
                    )
                ip_timeout = CounterMeasures.retrieve_timeout(request.remote_addr)
                if ip_timeout:
                    return render_template(
                        "ipaddr_ban_error.html",
                        page_title="IP Address Banned",
                        use_recaptcha=False,
                    )
            except Exception as err:
                logger.error(err)
                return render_template(
                    "recaptcha_error.html",
                    page_title="Recaptcha Submit Error!",
                    use_recaptcha=False,
                )
        api_key = secrets.token_urlsafe(16)
        hashed_api_key = hash_api_key(api_key)
        if user:
            username = sanitize_string(request.form["username"])
            if is_profane(username):
                return render_template("bad_username.html", page_title="Bad Username")
            user.username = username
            user.api_key = hashed_api_key
            db.session.commit()
        else:
            # Triggered when the user created a username without logging in
            if is_profane(request.form["username"]):
                return render_template("bad_username.html", page_title="Bad Username")
            if not oauth_id:
                oauth_id = str(uuid4())
                pseudonymous = True
                if settings.mode_raid():
                    return render_template(
                        "error.html",
                        page_title="Not Allowed",
                        error_message="We cannot allow anonymous registrations at the moment. "
                        "Please use one of the oauth2 buttons to login first.",
                    )
            username = sanitize_string(request.form["username"])
            user = User(username=username, oauth_id=oauth_id, api_key=hashed_api_key)
            user.create()
    if user:
        welcome = f"Welcome back {user.get_unique_alias()}"
    return render_template(
        "register.html",
        page_title=f"Join the {horde_title}!",
        use_recaptcha=use_recaptcha,
        recaptcha_site=os.getenv("RECAPTCHA_SITE_KEY"),
        welcome=welcome,
        user=user,
        api_key=api_key,
        username=username,
        pseudonymous=pseudonymous,
        oauth_id=oauth_id,
    )


@logger.catch(reraise=True)
@HORDE.route("/transfer", methods=["GET", "POST"])
def transfer():
    src_user = None
    dest_username = None
    kudos = None
    error = None
    welcome = "Welcome"
    oauth_id = get_oauth_id()
    if oauth_id:
        src_user = database.find_user_by_oauth_id(oauth_id)
        if not src_user:
            # This probably means the user was deleted
            oauth_id = None
    if request.method == "POST":
        dest_username = request.form["username"]
        amount = request.form["amount"]
        if not amount.isnumeric():
            kudos = 0
            error = "Please enter a number in the kudos field"
        # Triggered when the user submited without logging in
        elif src_user:
            ret = database.transfer_kudos_to_username(src_user, dest_username, int(amount))
            kudos = ret[0]
            error = ret[1]
        else:
            ret = database.transfer_kudos_from_apikey_to_username(
                request.form["src_api_key"],
                dest_username,
                int(amount),
            )
            kudos = ret[0]
            error = ret[1]
    if src_user:
        welcome = f"Welcome back {src_user.get_unique_alias()}. You have {src_user.kudos} kudos remaining"
    return render_template(
        "transfer_kudos.html",
        page_title="Kudos Transfer",
        welcome=welcome,
        kudos=kudos,
        error=error,
        dest_username=dest_username,
        oauth_id=oauth_id,
    )


@HORDE.route("/google/<return_to>")
def google_login(return_to):
    global dance_return_to
    dance_return_to = "/" + return_to
    return redirect(url_for("google.login"))


@HORDE.route("/discord/<return_to>")
def discord_login(return_to):
    global dance_return_to
    dance_return_to = "/" + return_to
    return redirect(url_for("discord.login"))


@HORDE.route("/github/<return_to>")
def github_login(return_to):
    global dance_return_to
    dance_return_to = "/" + return_to
    return redirect(url_for("github.login"))


# @HORDE.route('/patreon/<return_to>')
# def patreon_login(return_to):
#     global dance_return_to
#     dance_return_to = '/' + return_to
#     return redirect('/patreon/patreon')


@HORDE.route("/finish_dance")
def finish_dance():
    global dance_return_to
    redirect_url = dance_return_to
    dance_return_to = "/"
    return redirect(redirect_url)


def _clean_doc_name(env_var_name: str, default: str) -> str:
    """Return a safe template name, stripping any stray quote characters.

    Some environments may accidentally set HORDE_HTML_* values with smart quotes
    or surrounding quotes (e.g. “terms_of_service.html"). This helper normalizes
    them so Jinja can resolve the correct template.
    """
    raw = os.getenv(env_var_name, default) or default
    # Strip common quote characters from both ends
    return raw.strip(" \"'“”‘’")


@HORDE.route("/privacy")
def privacy():
    return render_template(
        "document.html",
        doc=_clean_doc_name("HORDE_HTML_PRIVACY", "privacy_policy.html"),
        horde_title=horde_title,
        horde_url=horde_url,
        horde_contact_email=horde_contact_email,
    )


@HORDE.route("/terms")
def terms():
    return render_template(
        "document.html",
        doc=_clean_doc_name("HORDE_HTML_TERMS", "terms_of_service.html"),
        horde_title=horde_title,
        horde_url=horde_url,
        horde_contact_email=horde_contact_email,
    )


@HORDE.route("/assets/<filename>")
def assets(filename):
    return send_from_directory("../assets", filename)


@HORDE.route("/.well-known/serviceinfo")
def serviceinfo():
    return {
        "version": "0.2",
        "software": {
            "name": horde_title,
            "version": HORDE_VERSION,
            "repository": horde_repository,
            "homepage": horde_url,
            "logo": horde_logo,
        },
        "api": {
            "aihorde": {
                "name": "AI Horde API",
                "version": HORDE_API_VERSION,
                "base_url": f"{horde_url}/api/v2",
                "rel_url": "/api/v2",
                "documentation": f"{horde_url}/api",
            },
        },
    }, 200


@HORDE.route("/test-proxy")
def test_proxy():
    """Simple test route to verify routing is working."""
    return {"status": "proxy test route works"}, 200


@HORDE.route("/simple-proxy")
def simple_proxy():
    """Simple test proxy route."""
    return {"status": "simple proxy works"}, 200


@HORDE.route("/test-param/<param>")
def test_param(param: str):
    """Test route with parameter."""
    return {"param": param, "status": "param route works"}, 200


@HORDE.route("/test-methods/<param>", methods=["GET", "POST"])
def test_methods(param: str):
    """Test route with parameter and methods."""
    return {"param": param, "method": request.method, "status": "methods route works"}, 200


@HORDE.route("/proxy-api/<path:subpath>", methods=["GET", "POST"])
def proxy_api(subpath: str):
    """Same-origin proxy to production API to avoid browser CORS/preflight issues."""
    try:
        target = f"https://api.aipowergrid.io/{subpath}"
        headers = {"Content-Type": "application/json"}
        api_key = request.headers.get("apikey")
        if api_key:
            headers["apikey"] = api_key

        if request.method == "POST":
            payload = request.get_json(force=True, silent=True)
            resp = requests.post(target, headers=headers, json=payload, timeout=60)
        else:
            resp = requests.get(target, headers=headers, timeout=60)

        content_type = resp.headers.get("Content-Type", "application/json")
        return resp.text, resp.status_code, {"Content-Type": content_type}
    except Exception as exc:
        err = {"error": str(exc)}
        return json.dumps(err), 500, {"Content-Type": "application/json"}


@HORDE.route("/debug-routes")
def debug_routes():
    routes_list = []
    for rule in sorted(HORDE.url_map.iter_rules(), key=lambda r: r.rule):
        methods = ",".join(sorted(m for m in rule.methods if m not in {"HEAD", "OPTIONS"}))
        routes_list.append(f"{rule.rule} [{methods}] -> {rule.endpoint}")
    return "\n".join(routes_list), 200, {"Content-Type": "text/plain; charset=utf-8"}
