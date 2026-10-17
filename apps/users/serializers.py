"""Serializers for users, PIN authentication and profile management."""
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from .models import Language, PinRecoveryRequest, User, UserRole, pin_validator
from .services import authenticate_pin, normalize_identifier


class UserSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(read_only=True)
    is_staff_role = serializers.BooleanField(read_only=True)

    class Meta:
        model = User
        fields = [
            "id",
            "phone",
            "username",
            "first_name",
            "last_name",
            "full_name",
            "email",
            "role",
            "language",
            "is_staff_role",
            "is_active",
            "is_blocked",
            "created_at",
        ]
        read_only_fields = ["id", "role", "is_active", "is_blocked", "created_at"]


class LoginSerializer(serializers.Serializer):
    identifier = serializers.CharField(
        label=_("Телефон или логин"), max_length=150, help_text=_("Номер телефона или логин пользователя")
    )
    pin = serializers.CharField(label=_("ПИН-код"), max_length=6, min_length=4, write_only=True)

    def validate_identifier(self, value: str) -> str:
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError(_("Укажите телефон или логин."))
        return value

    def validate_pin(self, value: str) -> str:
        pin_validator(value)
        return value

    def validate(self, attrs: dict) -> dict:
        attrs["user"] = authenticate_pin(attrs["identifier"], attrs["pin"])
        return attrs


class ChangePinSerializer(serializers.Serializer):
    old_pin = serializers.CharField(label=_("Текущий ПИН"), max_length=6, min_length=4, write_only=True)
    new_pin = serializers.CharField(label=_("Новый ПИН"), max_length=6, min_length=4, write_only=True)
    new_pin_confirm = serializers.CharField(
        label=_("Повторите новый ПИН"), max_length=6, min_length=4, write_only=True
    )

    def validate_old_pin(self, value: str) -> str:
        pin_validator(value)
        if not self.context["request"].user.check_pin(value):
            raise serializers.ValidationError(_("Текущий ПИН-код указан неверно."))
        return value

    def validate_new_pin(self, value: str) -> str:
        pin_validator(value)
        return value

    def validate(self, attrs: dict) -> dict:
        if attrs["new_pin"] != attrs["new_pin_confirm"]:
            raise serializers.ValidationError({"new_pin_confirm": _("ПИН-коды не совпадают.")})
        if attrs["new_pin"] == attrs["old_pin"]:
            raise serializers.ValidationError({"new_pin": _("Новый ПИН-код должен отличаться от текущего.")})
        return attrs

    def save(self, **kwargs) -> User:
        user = self.context["request"].user
        user.set_pin(self.validated_data["new_pin"])
        user.save(update_fields=["pin", "pin_set_at", "failed_pin_attempts", "pin_locked_until", "updated_at"])
        return user


class ProfileUpdateSerializer(serializers.ModelSerializer):
    language = serializers.ChoiceField(choices=Language.choices, required=False)

    class Meta:
        model = User
        fields = ["first_name", "last_name", "email", "language"]


class PinRecoveryRequestSerializer(serializers.ModelSerializer):
    user_info = serializers.SerializerMethodField()
    user = serializers.PrimaryKeyRelatedField(queryset=User.objects.all(), required=False)

    class Meta:
        model = PinRecoveryRequest
        fields = [
            "id",
            "user",
            "user_info",
            "comment",
            "status",
            "decision_comment",
            "created_at",
            "decided_at",
        ]
        read_only_fields = ["id", "status", "decision_comment", "created_at", "decided_at"]

    def get_user_info(self, obj: PinRecoveryRequest) -> str:
        return str(obj.user)

    def create(self, validated_data: dict) -> PinRecoveryRequest:
        validated_data["user"] = self.context["request"].user
        return super().create(validated_data)


class PinRecoveryDecisionSerializer(serializers.Serializer):
    new_pin = serializers.CharField(
        label=_("Новый ПИН-код"), max_length=6, min_length=4, required=False, write_only=True
    )
    decision_comment = serializers.CharField(label=_("Комментарий"), required=False, allow_blank=True)

    def validate_new_pin(self, value: str) -> str:
        pin_validator(value)
        return value


class UserAdminSerializer(serializers.ModelSerializer):
    """Создание/обновление пользователей администратором."""

    pin = serializers.CharField(label=_("ПИН-код"), max_length=6, min_length=4, write_only=True, required=False)
    full_name = serializers.CharField(read_only=True)

    class Meta:
        model = User
        fields = [
            "id",
            "phone",
            "username",
            "first_name",
            "last_name",
            "full_name",
            "email",
            "role",
            "language",
            "pin",
            "is_active",
            "is_blocked",
            "created_at",
        ]
        read_only_fields = ["id", "created_at"]

    def validate_phone(self, value: str) -> str:
        value = normalize_identifier(value)
        queryset = User.objects.filter(phone=value)
        if self.instance:
            queryset = queryset.exclude(pk=self.instance.pk)
        if queryset.exists():
            raise serializers.ValidationError(_("Пользователь с таким телефоном уже существует."))
        return value

    def validate_role(self, value: str) -> str:
        request = self.context.get("request")
        if request and request.user.role == UserRole.ADMIN and value == UserRole.SUPER_ADMIN:
            raise serializers.ValidationError(_("Только супер-администратор может назначать эту роль."))
        return value

    def create(self, validated_data: dict) -> User:
        pin = validated_data.pop("pin", None)
        if not pin:
            raise serializers.ValidationError({"pin": _("Укажите ПИН-код для нового пользователя.")})
        user = User.objects.create_user(pin=pin, **validated_data)
        return user

    def update(self, instance: User, validated_data: dict) -> User:
        pin = validated_data.pop("pin", None)
        user = super().update(instance, validated_data)
        if pin:
            user.set_pin(pin)
            user.save(update_fields=["pin", "pin_set_at", "failed_pin_attempts", "pin_locked_until"])
        return user
