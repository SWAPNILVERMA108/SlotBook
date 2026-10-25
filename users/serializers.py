from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

User = get_user_model()


class RegisterSerializer(serializers.ModelSerializer):
    """Checks the data a new user sends and creates the account."""

    # write_only: the password is accepted from the client but NEVER sent back.
    password = serializers.CharField(write_only=True, style={"input_type": "password"})

    class Meta:
        model = User
        fields = ["id", "username", "email", "phone", "password"]
        # Django's default User allows a blank email. For our app, email is required.
        extra_kwargs = {"email": {"required": True, "allow_blank": False}}

    def validate_email(self, value):
        # iexact = ignore upper/lower case, so "A@x.com" and "a@x.com" count as the same.
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("A user with this email already exists.")
        return value.lower()

    def validate(self, attrs):
        # Run Django's password rules (length, too common, all numbers, similar to username).
        candidate = User(username=attrs.get("username", ""), email=attrs.get("email", ""))
        try:
            validate_password(attrs["password"], user=candidate)
        except DjangoValidationError as error:
            raise serializers.ValidationError({"password": list(error.messages)}) from None
        return attrs

    def create(self, validated_data):
        # create_user hashes the password. Never use User.objects.create(password=...)!
        return User.objects.create_user(**validated_data)


class UserSerializer(serializers.ModelSerializer):
    """What we show about the logged-in user (no password, of course)."""

    class Meta:
        model = User
        fields = ["id", "username", "email", "phone", "date_joined"]
        read_only_fields = fields
