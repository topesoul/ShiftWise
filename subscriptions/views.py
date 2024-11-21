# /workspace/shiftwise/subscriptions/views.py

import logging
from collections import defaultdict

import stripe
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import TemplateView

from accounts.models import Agency, Profile
from core.mixins import AgencyOwnerRequiredMixin
from subscriptions.models import Plan, Subscription

from .utils import create_stripe_customer

# Initialize logger
logger = logging.getLogger(__name__)

# Set Stripe API key
stripe.api_key = settings.STRIPE_SECRET_KEY


class SubscriptionHomeView(LoginRequiredMixin, TemplateView):
    """
    Displays the available subscription plans and the agency's current subscription status.
    Accessible to authenticated users.
    """

    template_name = "subscriptions/subscription_home.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user

        if user.is_authenticated:
            try:
                profile = user.profile
            except Profile.DoesNotExist:
                messages.error(
                    self.request, "User profile does not exist. Please contact support."
                )
                logger.error(f"Profile does not exist for user: {user.username}")
                return context

            agency = profile.agency

            if agency is None:
                messages.error(
                    self.request, "Your agency information is missing. Please contact support."
                )
                logger.error(f"Agency is None for user: {user.username}")
                return context

            # Get current subscription
            try:
                subscription = agency.subscription
                if (
                    subscription.is_active
                    and subscription.current_period_end
                    and subscription.current_period_end > timezone.now()
                ):
                    context["subscription"] = subscription
                else:
                    context["subscription"] = None
            except Subscription.DoesNotExist:
                context["subscription"] = None
                logger.warning(f"No active subscription for agency: {agency.name}")
            except Exception as e:
                messages.error(
                    self.request, "An error occurred while retrieving your subscription."
                )
                logger.exception(f"Error retrieving subscription: {e}")
                context["subscription"] = None

            # Retrieve all active plans
            plans = Plan.objects.filter(is_active=True).order_by("name", "billing_cycle")

            # Group plans by name
            plan_dict = defaultdict(dict)
            for plan in plans:
                if plan.billing_cycle.lower() == "monthly":
                    plan_dict[plan.name]["monthly_plan"] = plan
                elif plan.billing_cycle.lower() == "yearly":
                    plan_dict[plan.name]["yearly_plan"] = plan

            # Structure available_plans as a list of dictionaries
            available_plans = []
            for plan_name, plans in plan_dict.items():
                # Ensure at least one plan exists
                if not plans.get("monthly_plan") and not plans.get("yearly_plan"):
                    logger.warning(f"No monthly or yearly plan found for {plan_name}. Skipping.")
                    continue

                # Use the description from either monthly or yearly plan
                description = (
                    plans.get("monthly_plan").description
                    if plans.get("monthly_plan")
                    else plans.get("yearly_plan").description
                )

                available_plans.append(
                    {
                        "name": plan_name,
                        "description": description,
                        "monthly_plan": plans.get("monthly_plan"),
                        "yearly_plan": plans.get("yearly_plan"),
                    }
                )

            # Log available plans for debugging
            logger.debug(f"Available Plans: {[plan['name'] for plan in available_plans]}")

            context["available_plans"] = available_plans

        return context


class SubscribeView(LoginRequiredMixin, View):
    """
    Handles the subscription process, integrating with Stripe Checkout.
    """

    def get(self, request, plan_id, *args, **kwargs):
        return self.process_subscription(request, plan_id)

    def post(self, request, plan_id, *args, **kwargs):
        return self.process_subscription(request, plan_id)

    def process_subscription(self, request, plan_id):
        user = request.user

        # Ensure the user has a profile
        try:
            profile = user.profile
        except Profile.DoesNotExist:
            messages.error(request, "Please complete your profile before subscribing.")
            logger.error(f"Profile does not exist for user: {user.username}")
            return redirect("accounts:update_profile")

        # Ensure the user has an agency
        agency = profile.agency
        if not agency:
            messages.error(request, "Please create an agency before subscribing.")
            logger.error(f"Agency is None for user: {user.username}")
            return redirect("accounts:create_agency")

        # Check if user is an agency owner
        if not user.groups.filter(name="Agency Owners").exists():
            messages.error(request, "Only agency owners can subscribe.")
            logger.warning(f"User {user.username} attempted to subscribe without being an agency owner.")
            return redirect("subscriptions:subscription_home")

        # Get the selected plan
        plan = get_object_or_404(Plan, id=plan_id, is_active=True)

        # At this point, the Stripe customer should already be created via signals.py
        if not agency.stripe_customer_id:
            messages.error(request, "Stripe customer ID is missing. Please contact support.")
            logger.error(f"Stripe customer ID is missing for agency: {agency.name}")
            return redirect("subscriptions:subscription_home")

        try:
            # Retrieve Existing Stripe Customer
            customer = stripe.Customer.retrieve(agency.stripe_customer_id)
            logger.info(
                f"Stripe customer retrieved for agency: {agency.name}, Customer ID: {customer.id}"
            )
        except stripe.error.StripeError as e:
            messages.error(request, "Failed to retrieve Stripe customer.")
            logger.exception(f"Stripe error while retrieving customer: {e}")
            return redirect("subscriptions:subscription_home")
        except Exception as e:
            messages.error(
                request, "An unexpected error occurred. Please try again."
            )
            logger.exception(f"Unexpected error while retrieving customer: {e}")
            return redirect("subscriptions:subscription_home")

        # Prevent Creating Duplicate Subscriptions
        if hasattr(agency, 'subscription') and agency.subscription.is_active:
            messages.info(request, "You already have an active subscription. Manage your subscription instead.")
            logger.info(f"Agency {agency.name} already has an active subscription.")
            return redirect("subscriptions:manage_subscription")

        # Create a Stripe Checkout Session
        try:
            checkout_session = stripe.checkout.Session.create(
                customer=customer.id,
                payment_method_types=["card"],
                line_items=[
                    {
                        "price": plan.stripe_price_id,
                        "quantity": 1,
                    },
                ],
                mode="subscription",
                success_url=request.build_absolute_uri(
                    reverse("subscriptions:subscription_success")
                ),
                cancel_url=request.build_absolute_uri(
                    reverse("subscriptions:subscription_cancel")
                ),
            )
            logger.info(
                f"Stripe Checkout Session created: {checkout_session.id} for agency: {agency.name}"
            )
            return redirect(checkout_session.url)
        except stripe.error.StripeError as e:
            messages.error(request, "There was an error creating the checkout session.")
            logger.exception(f"Stripe error while creating checkout session: {e}")
            return redirect("subscriptions:subscription_home")
        except Exception as e:
            messages.error(request, "An unexpected error occurred. Please try again.")
            logger.exception(f"Unexpected error while creating checkout session: {e}")
            return redirect("subscriptions:subscription_home")


def subscription_success(request):
    """
    Renders the subscription success page.
    """
    messages.success(request, "Your subscription was successful!")
    return render(request, "subscriptions/success.html")


def subscription_cancel(request):
    """
    Renders the subscription cancellation page.
    """
    messages.error(request, "Your subscription was cancelled.")
    return render(request, "subscriptions/cancel.html")


@method_decorator(csrf_exempt, name="dispatch")
class StripeWebhookView(View):
    """
    Handles incoming Stripe webhooks.
    """

    def post(self, request, *args, **kwargs):
        payload = request.body
        sig_header = request.META.get("HTTP_STRIPE_SIGNATURE")
        endpoint_secret = settings.STRIPE_WEBHOOK_SECRET

        try:
            event = stripe.Webhook.construct_event(
                payload, sig_header, endpoint_secret
            )
            logger.info(f"Stripe webhook received: {event['type']}")
        except ValueError as e:
            # Invalid payload
            logger.exception(f"Invalid payload: {e}")
            return HttpResponse(status=400)
        except stripe.error.SignatureVerificationError as e:
            # Invalid signature
            logger.exception(f"Invalid signature: {e}")
            return HttpResponse(status=400)

        # Handle the event
        if event["type"] == "checkout.session.completed":
            session = event["data"]["object"]
            self.handle_checkout_session_completed(session)
        elif event["type"] == "invoice.paid":
            invoice = event["data"]["object"]
            self.handle_invoice_paid(invoice)
        elif event["type"] == "customer.subscription.deleted":
            subscription = event["data"]["object"]
            self.handle_subscription_deleted(subscription)
        elif event["type"] == "customer.subscription.updated":
            subscription = event["data"]["object"]
            self.handle_subscription_updated(subscription)
        else:
            logger.info(f"Unhandled event type: {event['type']}")

        return HttpResponse(status=200)

    def handle_checkout_session_completed(self, session):
        """
        Handles the checkout.session.completed event.
        """
        customer_id = session.get("customer")
        subscription_id = session.get("subscription")

        logger.debug(f"Handling checkout.session.completed for customer {customer_id}")

        try:
            # Retrieve the Stripe Subscription object
            stripe_subscription = stripe.Subscription.retrieve(subscription_id)
            plan_id = stripe_subscription["items"]["data"][0]["price"]["id"]
            current_period_start = timezone.datetime.fromtimestamp(
                stripe_subscription["current_period_start"], tz=timezone.utc
            )
            current_period_end = timezone.datetime.fromtimestamp(
                stripe_subscription["current_period_end"], tz=timezone.utc
            )

            agency = Agency.objects.get(stripe_customer_id=customer_id)
            plan = Plan.objects.get(stripe_price_id=plan_id)

            try:
                # Try to get the existing subscription
                subscription = agency.subscription
                # Update existing subscription
                subscription.plan = plan
                subscription.stripe_subscription_id = subscription_id
                subscription.is_active = True
                subscription.status = stripe_subscription["status"]
                subscription.current_period_start = current_period_start
                subscription.current_period_end = current_period_end
                subscription.is_expired = False
            except Subscription.DoesNotExist:
                # Create a new subscription if none exists
                subscription = Subscription(
                    agency=agency,
                    plan=plan,
                    stripe_subscription_id=subscription_id,
                    is_active=True,
                    status=stripe_subscription["status"],
                    current_period_start=current_period_start,
                    current_period_end=current_period_end,
                    is_expired=False,
                )

            # Perform validation before saving
            subscription.full_clean()
            subscription.save()
            logger.info(f"Subscription updated for agency {agency.name}")

        except Agency.DoesNotExist:
            logger.exception(f"Agency with customer ID {customer_id} does not exist.")
            return HttpResponse(status=400)
        except Plan.DoesNotExist:
            logger.exception(f"Plan with price ID {plan_id} does not exist.")
            return HttpResponse(status=400)
        except ValidationError as ve:
            logger.exception(f"Validation error while updating subscription: {ve}")
            return HttpResponse(status=400)
        except Exception as e:
            logger.exception(f"Unexpected error while handling checkout session: {e}")
            return HttpResponse(status=400)

    def handle_invoice_paid(self, invoice):
        """
        Handles the invoice.paid event.
        """
        logger.info(f"Invoice paid: {invoice.id}")
        # Implement any necessary logic for invoice paid
        pass

    def handle_subscription_deleted(self, subscription):
        """
        Handles the customer.subscription.deleted event.
        """
        stripe_subscription_id = subscription.get("id")
        try:
            local_subscription = Subscription.objects.get(
                stripe_subscription_id=stripe_subscription_id
            )
            local_subscription.is_active = False
            local_subscription.status = 'canceled'
            local_subscription.save()
            logger.info(f"Subscription {stripe_subscription_id} deactivated.")
        except Subscription.DoesNotExist:
            logger.exception(f"Subscription with ID {stripe_subscription_id} does not exist.")
        except Exception as e:
            logger.exception(f"Unexpected error while handling subscription deletion: {e}")

    def handle_subscription_updated(self, subscription):
        """
        Handles the customer.subscription.updated event.
        """
        stripe_subscription_id = subscription.get("id")
        try:
            local_subscription = Subscription.objects.get(
                stripe_subscription_id=stripe_subscription_id
            )
            # Update subscription details
            local_subscription.is_active = subscription.get("status") == "active"
            local_subscription.status = subscription.get("status", local_subscription.status)
            current_period_end = subscription.get("current_period_end")
            if current_period_end:
                local_subscription.current_period_end = timezone.datetime.fromtimestamp(
                    current_period_end, tz=timezone.utc
                )
            # Update plan if changed
            new_plan_id = subscription["items"]["data"][0]["price"]["id"]
            new_plan = Plan.objects.get(stripe_price_id=new_plan_id)
            local_subscription.plan = new_plan
            local_subscription.save()
            logger.info(
                f"Subscription updated to {new_plan.name} for agency: {local_subscription.agency.name}"
            )
        except Subscription.DoesNotExist:
            logger.exception(f"Subscription with ID {stripe_subscription_id} does not exist.")
        except Plan.DoesNotExist:
            logger.exception(
                f"Plan with price ID {subscription['items']['data'][0]['price']['id']} does not exist."
            )
        except ValidationError as ve:
            logger.exception(f"Validation error while updating subscription: {ve}")
        except Exception as e:
            logger.exception(f"Unexpected error while handling subscription update: {e}")


stripe_webhook = StripeWebhookView.as_view()


class SubscriptionChangeView(LoginRequiredMixin, AgencyOwnerRequiredMixin, TemplateView):
    """
    Base class for handling subscription changes (upgrade/downgrade).
    """

    template_name = "subscriptions/subscription_form_base.html"
    change_type = None  # 'upgrade' or 'downgrade'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user

        if not self.change_type:
            raise NotImplementedError("Change type must be defined in subclasses.")

        try:
            profile = user.profile
            agency = profile.agency
            subscription = agency.subscription
            if (
                subscription.is_active
                and subscription.current_period_end
                and subscription.current_period_end > timezone.now()
            ):
                context["current_plan"] = subscription.plan
            else:
                messages.error(
                    self.request, "Active subscription not found. Please subscribe first."
                )
                logger.error(f"No active subscription for agency: {agency.name if agency else 'N/A'}")
                return context

            if self.change_type == 'upgrade':
                available_plans = Plan.objects.filter(
                    price__gt=subscription.plan.price,
                    is_active=True
                ).order_by("price")
                form_title = "Upgrade Your Subscription"
                button_label = "Upgrade Subscription"
            elif self.change_type == 'downgrade':
                available_plans = Plan.objects.filter(
                    price__lt=subscription.plan.price,
                    is_active=True
                ).order_by("-price")
                form_title = "Downgrade Your Subscription"
                button_label = "Downgrade Subscription"
            else:
                available_plans = []
                form_title = "Change Subscription"
                button_label = "Change Subscription"

            context["available_plans"] = available_plans
            context["form_title"] = form_title
            context["button_label"] = button_label

        except Profile.DoesNotExist:
            messages.error(
                self.request, "User profile does not exist. Please contact support."
            )
            logger.error(f"Profile does not exist for user: {user.username}")
        except Subscription.DoesNotExist:
            messages.error(
                self.request, "Active subscription not found. Please subscribe first."
            )
            logger.error(f"No active subscription for agency: {agency.name if agency else 'N/A'}")
        except Exception as e:
            messages.error(
                self.request, "An unexpected error occurred. Please try again."
            )
            logger.exception(f"Unexpected error in SubscriptionChangeView: {e}")

        return context

    def post(self, request, *args, **kwargs):
        if not self.change_type:
            messages.error(request, "Invalid subscription change type.")
            logger.error("SubscriptionChangeView called without a valid change_type.")
            return redirect("subscriptions:subscription_home")

        plan_id = request.POST.get("plan_id")
        new_plan = get_object_or_404(Plan, id=plan_id, is_active=True)

        user = request.user

        try:
            profile = user.profile
            agency = profile.agency
            subscription = agency.subscription

            # Update Stripe subscription
            stripe_subscription = stripe.Subscription.retrieve(subscription.stripe_subscription_id)
            current_item_id = stripe_subscription["items"]["data"][0].id

            stripe.Subscription.modify(
                subscription.stripe_subscription_id,
                cancel_at_period_end=False,
                items=[
                    {
                        "id": current_item_id,
                        "price": new_plan.stripe_price_id,
                    }
                ],
                proration_behavior='create_prorations',  # Adjust proration as needed
            )

            # Update local subscription
            subscription.plan = new_plan
            subscription.save()

            action = "upgraded" if self.change_type == 'upgrade' else "downgraded"
            messages.success(
                request, f"Subscription {action} to {new_plan.name} plan successfully."
            )
            logger.info(
                f"Subscription {action} to {new_plan.name} by user: {user.username}"
            )
            return redirect("subscriptions:manage_subscription")

        except Subscription.DoesNotExist:
            messages.error(
                request, "Active subscription not found. Please subscribe first."
            )
            logger.error(f"No active subscription for agency: {agency.name if agency else 'N/A'}")
            return redirect("subscriptions:subscription_home")
        except stripe.error.StripeError as e:
            messages.error(
                request,
                "An error occurred while changing your subscription. Please try again.",
            )
            logger.exception(f"Stripe error during subscription change: {e}")
            return redirect("subscriptions:subscription_home")
        except Exception as e:
            messages.error(request, "An unexpected error occurred. Please try again.")
            logger.exception(f"Unexpected error during subscription change: {e}")
            return redirect("subscriptions:subscription_home")


class UpgradeSubscriptionView(SubscriptionChangeView):
    """
    Allows agency owners to upgrade their subscription plans.
    """
    change_type = 'upgrade'


class DowngradeSubscriptionView(SubscriptionChangeView):
    """
    Allows agency owners to downgrade their subscription plans.
    """
    change_type = 'downgrade'


class CancelSubscriptionView(LoginRequiredMixin, AgencyOwnerRequiredMixin, View):
    """
    Allows agency owners to cancel their subscriptions.
    """

    def post(self, request, *args, **kwargs):
        user = request.user

        # Ensure the user has a profile
        try:
            profile = user.profile
        except Profile.DoesNotExist:
            messages.error(
                request, "User profile does not exist. Please contact support."
            )
            logger.error(f"Profile does not exist for user: {user.username}")
            return redirect("subscriptions:subscription_home")

        agency = profile.agency

        if not agency:
            messages.error(
                request,
                "Your agency information is missing. Please contact support.",
            )
            logger.error(f"Agency is None for user: {user.username}")
            return redirect("subscriptions:subscription_home")

        if not agency.stripe_customer_id:
            messages.error(
                request, "No Stripe customer ID found. Please contact support."
            )
            logger.error(f"Stripe customer ID is missing for agency: {agency.name}")
            return redirect("subscriptions:subscription_home")

        try:
            # Fetch active subscriptions
            subscriptions = stripe.Subscription.list(customer=agency.stripe_customer_id, status='active')

            for subscription in subscriptions.auto_paging_iter():
                stripe.Subscription.delete(subscription.id)
                # Update local subscription record
                local_subscription = Subscription.objects.filter(
                    stripe_subscription_id=subscription.id
                ).first()
                if local_subscription:
                    local_subscription.is_active = False
                    local_subscription.status = 'canceled'
                    local_subscription.save()
                    logger.info(f"Subscription {subscription.id} deactivated for agency: {agency.name}")

            messages.success(request, "Your subscription has been cancelled.")
            return redirect("subscriptions:subscription_home")
        except stripe.error.StripeError as e:
            messages.error(request, "Unable to cancel your subscription.")
            logger.exception(f"Stripe error while cancelling subscription: {e}")
            return redirect("subscriptions:subscription_home")
        except Exception as e:
            messages.error(request, "An unexpected error occurred. Please try again.")
            logger.exception(f"Unexpected error while cancelling subscription: {e}")
            return redirect("subscriptions:subscription_home")


class ManageSubscriptionView(LoginRequiredMixin, AgencyOwnerRequiredMixin, TemplateView):
    """
    Allows agency owners to manage their subscriptions.
    """

    template_name = "subscriptions/manage_subscription.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user

        try:
            profile = user.profile
        except Profile.DoesNotExist:
            messages.error(
                self.request, "User profile does not exist. Please contact support."
            )
            logger.error(f"Profile does not exist for user: {user.username}")
            return context

        agency = profile.agency

        if not agency:
            messages.error(
                self.request,
                "Your agency information is missing. Please contact support.",
            )
            logger.error(f"Agency is None for user: {user.username}")
            return context

        if not agency.stripe_customer_id:
            messages.error(
                self.request, "No Stripe customer ID found. Please contact support."
            )
            logger.error(f"Stripe customer ID is missing for agency: {agency.name}")
            return context

        try:
            # Fetch subscription from local database
            try:
                subscription = agency.subscription
                if (
                    subscription.is_active
                    and subscription.current_period_end
                    and subscription.current_period_end > timezone.now()
                ):
                    context["subscription"] = subscription
                else:
                    context["subscription"] = None
            except Subscription.DoesNotExist:
                context["subscription"] = None
                logger.warning(f"No active subscription for agency: {agency.name}")

            # Fetch subscriptions from Stripe
            subscriptions = stripe.Subscription.list(customer=agency.stripe_customer_id, limit=10)
            context["subscriptions"] = subscriptions

            # Generate Billing Portal session link
            billing_portal_session = stripe.billing_portal.Session.create(
                customer=agency.stripe_customer_id,
                return_url=self.request.build_absolute_uri(reverse("subscriptions:manage_subscription"))
            )
            context["billing_portal_url"] = billing_portal_session.url

        except stripe.error.StripeError as e:
            messages.error(self.request, "Unable to retrieve subscription details.")
            logger.exception(f"Stripe error while retrieving subscriptions: {e}")
            return redirect("subscriptions:subscription_home")
        except Exception as e:
            messages.error(
                self.request, "An unexpected error occurred. Please try again."
            )
            logger.exception(f"Unexpected error while retrieving subscriptions: {e}")
            return redirect("subscriptions:subscription_home")

        return context


class UpdatePaymentMethodView(LoginRequiredMixin, AgencyOwnerRequiredMixin, View):
    """
    Allows agency owners to update their payment methods via Stripe's Billing Portal.
    """

    def get(self, request, *args, **kwargs):
        user = request.user

        try:
            profile = user.profile
        except Profile.DoesNotExist:
            messages.error(
                request, "User profile does not exist. Please contact support."
            )
            logger.error(f"Profile does not exist for user: {user.username}")
            return redirect("subscriptions:subscription_home")

        agency = profile.agency

        if not agency:
            messages.error(
                request, "Your agency information is missing. Please contact support."
            )
            logger.error(f"Agency is None for user: {user.username}")
            return redirect("subscriptions:subscription_home")

        if not agency.stripe_customer_id:
            messages.error(
                request, "No Stripe customer ID found. Please contact support."
            )
            logger.error(f"Stripe customer ID is missing for agency: {agency.name}")
            return redirect("subscriptions:subscription_home")

        try:
            # Create a Billing Portal session
            session = stripe.billing_portal.Session.create(
                customer=agency.stripe_customer_id,
                return_url=self.request.build_absolute_uri(reverse("subscriptions:manage_subscription"))
            )
            logger.info(f"Billing Portal session created: {session.id} for agency: {agency.name}")
            return redirect(session.url)
        except stripe.error.StripeError as e:
            messages.error(request, "Unable to redirect to Billing Portal.")
            logger.exception(f"Stripe error while creating Billing Portal session: {e}")
            return redirect("subscriptions:subscription_home")
        except Exception as e:
            messages.error(request, "An unexpected error occurred. Please try again.")
            logger.exception(f"Unexpected error while creating Billing Portal session: {e}")
            return redirect("subscriptions:subscription_home")