# Generated by Django 5.1.2 on 2024-11-09 16:25

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("subscriptions", "0009_plan_is_recommended"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="subscription",
            name="billing_address",
        ),
        migrations.RemoveField(
            model_name="subscription",
            name="payment_method",
        ),
        migrations.AddField(
            model_name="plan",
            name="stripe_product_id",
            field=models.CharField(blank=True, max_length=100, null=True, unique=True),
        ),
        migrations.AddField(
            model_name="plan",
            name="view_limit",
            field=models.PositiveIntegerField(
                blank=True,
                help_text="Maximum number of views allowed per month.",
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name="plan",
            name="stripe_price_id",
            field=models.CharField(blank=True, max_length=100, null=True, unique=True),
        ),
    ]