from functools import wraps
import hmac
import time

from flask import Blueprint, g, request, flash, session, redirect, url_for, \
    abort, jsonify
from pyisemail import is_email

from decorators import template_renderer, get_menu_entries
from mod_auth.forms import LoginForm, AccountForm, SignupForm, \
    CompleteSignupForm, ResetForm, CompleteResetForm, DeactivationForm, \
    RoleChangeForm
from mod_auth.models import Role, User

mod_auth = Blueprint('auth', __name__)


@mod_auth.before_app_request
def before_app_request():
    user_id = session.get('user_id', 0)
    g.user = User.query.filter(User.id == user_id).first()
    g.menu_entries['auth'] = {
        'title': 'Log in' if g.user is None else 'Log out',
        'icon': 'sign-in' if g.user is None else 'sign-out',
        'route': 'auth.login' if g.user is None else 'auth.logout'
    }
    g.menu_entries['account'] = {
        'title': 'Manage account',
        'icon': 'user',
        'route': 'auth.manage'
    }
    g.menu_entries['config'] = get_menu_entries(
        g.user, 'Platform mgmt', 'cog', [], '', [
            {'title': 'User manager', 'icon': 'users', 'route':
                'auth.users', 'access': [Role.admin]}
        ]
    )


def login_required(f):
    """
    Decorator that redirects to the login page if a user is not logged in.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if g.user is None:
            return redirect(url_for('auth.login',
                                    next=request.endpoint))
        return f(*args, **kwargs)
    return decorated_function


def check_access_rights(roles=None, parent_route=None):
    """
    Decorator that checks if a user can access the page.

    :param roles: A list of roles that can access the page.
    :type roles: list[str]
    :param parent_route: If the name of the route isn't a regular page (
    e.g. for ajax request handling), pass the name of the parent route.
    :type parent_route: str
    """
    if roles is None:
        roles = []

    def access_decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            route = parent_route
            if route is None:
                route = request.endpoint
            elif route.startswith("."):
                # Relative to current blueprint, so we'll need to adjust
                route = request.endpoint[:request.endpoint.rindex('.')] + \
                        route
            if g.user.role in roles:
                return f(*args, **kwargs)
            # Return page not allowed
            abort(403, request.endpoint)

        return decorated_function

    return access_decorator


def send_reset_email(usr):
    from run import app
    expires = int(time.time()) + 86400
    mac = hmac.new(
        app.config.get('HMAC_KEY', ''),
        "%s|%s|%s" % (usr.id, expires, usr.password)
    ).hexdigest()
    template = app.jinja_env.get_or_select_template(
        'email/recovery_link.txt')
    message = template.render(
        url=url_for('.complete_reset', uid=usr.id,
                    expires=expires, mac=mac, _external=True),
        name=usr.name
    )
    if not g.mailer.send_simple_message({
        "to": usr.email,
        "subject": "CCExtractor CI platform password recovery "
                   "instructions",
        "text": message
    }):
        flash('Could not send an email. Please get in touch',
              'error-message')


@mod_auth.route('/login', methods=['GET', 'POST'])
@template_renderer()
def login():
    form = LoginForm(request.form)
    redirect_location = request.args.get('next', '')
    if form.validate_on_submit():
        user = User.query.filter_by(email=form.email.data).first()

        if user and user.is_password_valid(form.password.data):
            session['user_id'] = user.id
            if len(redirect_location) == 0:
                return redirect("/")
            else:
                return redirect(url_for(redirect_location))

        flash('Wrong username or password', 'error-message')

    return {
        'next': redirect_location,
        'form': form
    }


@mod_auth.route('/reset', methods=['GET', 'POST'])
@template_renderer()
def reset():
    form = ResetForm(request.form)
    if form.validate_on_submit():
        user = User.query.filter_by(email=form.email.data).first()
        if user is not None:
            send_reset_email(user)
        flash('If an account was linked to the provided email address, '
              'an email with reset instructions has been sent. Please check '
              'your inbox.', 'success')
        form = ResetForm(None)
    return {
        'form': form
    }


@mod_auth.route('/reset/<uid>/<expires>/<mac>', methods=['GET', 'POST'])
@template_renderer()
def complete_reset(uid, expires, mac):
    from run import app
    # Check if time expired
    now = int(time.time())
    if now <= int(expires):
        user = User.query.filter_by(id=uid).first()
        if user is not None:
            # Validate HMAC
            real_hash = hmac.new(
                app.config.get('HMAC_KEY', ''),
                "%s|%s|%s" % (uid, expires, user.password)
            ).hexdigest()
            try:
                authentic = hmac.compare_digest(real_hash,
                                                mac.encode('utf-8'))
            except AttributeError:
                # Older python version? Fallback which is less safe
                authentic = real_hash == mac
            if authentic:
                form = CompleteResetForm(request.form)
                if form.validate_on_submit():
                    user.password = User.generate_hash(form.password.data)
                    g.db.commit()
                    template = app.jinja_env.get_or_select_template(
                        'email/password_reset.txt')
                    message = template.render(name=user.name)
                    g.mailer.send_simple_message({
                        "to": user.email,
                        "subject": "CCExtractor CI platform password reset",
                        "text": message
                    })
                    session['user_id'] = user.id
                    return redirect("/")
                return {
                    'form': form,
                    'uid': uid,
                    'mac': mac,
                    'expires': expires
                }

    flash('The request to reset your password was invalid. Please enter your '
          'email again to start over.', 'error-message')
    return redirect(url_for('.reset'))


@mod_auth.route('/signup', methods=['GET', 'POST'])
@template_renderer()
def signup():
    from run import app
    form = SignupForm(request.form)
    if form.validate_on_submit():
        if is_email(form.email.data):
            # Check if user exists
            user = User.query.filter_by(email=form.email.data).first()
            if user is None:
                expires = int(time.time()) + 86400
                hmac_hash = hmac.new(
                    app.config.get('HMAC_KEY', ''),
                    "%s|%s" % (form.email.data, expires)
                ).hexdigest()
                # New user
                template = app.jinja_env.get_or_select_template(
                    'email/registration_email.txt')
                message = template.render(
                    url=url_for(
                        '.complete_signup',
                        email=form.email.data,
                        expires=expires,
                        mac=hmac_hash,
                        _external=True
                    )
                )
            else:
                # Existing user
                template = app.jinja_env.get_or_select_template(
                    'email/registration_existing.txt')
                message = template.render(
                    url=url_for('.reset', _external=True),
                    name=user.name
                )
            if g.mailer.send_simple_message({
                "to": form.email.data,
                "subject": "CCExtractor CI platform registration",
                "text": message
            }):
                flash('Email sent for verification purposes. Please check '
                      'your mailbox', 'success')
                form = SignupForm(None)
            else:
                flash('Could not send email', 'error-message')
        else:
            flash('Invalid email address!', 'error-message')
    return {
        'form': form
    }


@mod_auth.route('/complete_signup/<email>/<expires>/<mac>',
                methods=['GET', 'POST'])
@template_renderer()
def complete_signup(email, expires, mac):
    from run import app
    # Check if time expired
    now = int(time.time())
    if now <= int(expires):
        # Validate HMAC
        real_hash = hmac.new(app.config.get('HMAC_KEY', ''),
                             "%s|%s" % (email, expires)).hexdigest()
        try:
            authentic = hmac.compare_digest(real_hash,
                                            mac.encode('utf-8'))
        except AttributeError:
            # Older python version? Fallback which is less safe
            authentic = real_hash == mac
        if authentic:
            # Check if email already exists (sign up twice with same email)
            user = User.query.filter_by(email=email).first()
            if user is not None:
                flash('There is already a user with this email address '
                      'registered.', 'error-message')
                return redirect(url_for('.signup'))
            form = CompleteSignupForm()
            if form.validate_on_submit():
                user = User(form.name.data, email=email,
                            password=User.generate_hash(form.password.data))
                g.db.add(user)
                g.db.commit()
                session['user_id'] = user.id
                # Send email
                template = app.jinja_env.get_or_select_template(
                    'email/registration_ok.txt')
                message = template.render(name=user.name)
                g.mailer.send_simple_message({
                    "to": user.email,
                    "subject": "Welcome to the CCExtractor CI platform",
                    "text": message
                })
                return redirect('/')
            return {
                'form': form,
                'email': email,
                'expires': expires,
                'mac': mac
            }

    flash('The request to complete the registration was invalid. Please '
          'enter your email again to start over.', 'error-message')
    return redirect(url_for('.signup'))


@mod_auth.route('/logout')
@template_renderer()
def logout():
    # Destroy session variable
    session.pop('user_id', None)
    flash('You have been logged out', 'success')
    return redirect(url_for('.login'))


@mod_auth.route('/manage', methods=['GET', 'POST'])
@login_required
@template_renderer()
def manage():
    from run import app
    form = AccountForm(request.form, g.user)
    if form.validate_on_submit():
        user = User.query.filter(User.id == g.user.id).first()
        old_email = None
        password = False
        if user.email != form.email.data:
            old_email = user.email
            user.email = form.email.data
        if len(form.new_password.data) >= 10:
            password = True
            user.password = User.generate_hash(form.new_password.data)
        if user.name != form.name.data:
            user.name = form.name.data
        g.user = user
        g.db.commit()
        if old_email is not None:
            template = app.jinja_env.get_or_select_template(
                'email/email_changed.txt')
            message = template.render(name=user.name, email=user.email)
            g.mailer.send_simple_message({
                "to": [old_email, user.email],
                "subject": "CCExtractor CI platform email changed",
                "text": message
            })
        if password:
            template = app.jinja_env.get_or_select_template(
                'email/password_changed.txt')
            message = template.render(name=user.name)
            to = user.email if old_email is None else [old_email, user.email]
            g.mailer.send_simple_message({
                "to": to,
                "subject": "CCExtractor CI platform password changed",
                "text": message
            })
        flash('Settings saved')
    return {
        'form': form
    }


@mod_auth.route('/users')
@login_required
@check_access_rights([Role.admin])
@template_renderer()
def users():
    return {
        'users': User.query.order_by(User.name.asc())
    }


@mod_auth.route('/user/<uid>')
@login_required
@template_renderer()
def user(uid):
    # Only give access if the uid matches the user, or if the user is an admin
    if g.user.id == uid or g.user.role == Role.admin:
        usr = User.query.filter_by(id=uid).first()
        if usr is not None:
            return {
                'view_user': usr
            }
        abort(404)
    else:
        abort(403, request.endpoint)


@mod_auth.route('/reset_user/<uid>')
@login_required
@check_access_rights([Role.admin])
@template_renderer()
def reset_user(uid):
    # Only give access if the uid matches the user, or if the user is an admin
    if g.user.id == uid or g.user.role == Role.admin:
        usr = User.query.filter_by(id=uid).first()
        if usr is not None:
            send_reset_email(usr)
            return {
                'view_user': usr
            }
        abort(404)
    else:
        abort(403, request.endpoint)


@mod_auth.route('/role/<uid>', methods=['GET', 'POST'])
@login_required
@check_access_rights([Role.admin])
@template_renderer()
def role(uid):
    usr = User.query.filter_by(id=uid).first()
    if usr is not None:
        form = RoleChangeForm(request.form)
        form.role.choices = [(r.name, r.description) for r in Role.__iter__()]
        if form.validate_on_submit():
            # Update role
            usr.role = Role.from_string(form.role.data)
            g.db.commit()
            return redirect(url_for('.users'))
        form.role.data = usr.role.name
        return {
            'form': form,
            'view_user': usr
        }
    abort(404)


@mod_auth.route('/deactivate/<uid>', methods=['GET', 'POST'])
@login_required
@template_renderer()
def deactivate(uid):
    # Only give access if the uid matches the user, or if the user is an admin
    if g.user.id == uid or g.user.role == Role.admin:
        usr = User.query.filter_by(id=uid).first()
        if usr is not None:
            form = DeactivationForm(request.form)
            if form.validate_on_submit():
                # Deactivate user
                usr.name = "Anonymized %s" % usr.id
                usr.email = "unknown%s@ccextractor.org" % usr.id
                usr.password = User.create_random_password(16)
                g.db.commit()
                if g.user.role == Role.admin:
                    return redirect(url_for('.users'))
                else:
                    session.pop('user_id', None)
                    flash('Account deactivated.', 'success')
                    return redirect(url_for('.login'))
            return {
                'form': form,
                'view_user': usr
            }
        abort(404)
    else:
        abort(403, request.endpoint)