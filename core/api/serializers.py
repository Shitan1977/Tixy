from django.template.context_processors import request
from rest_framework import serializers
from django.contrib.auth import get_user_model
from .models import *
from .utils import invia_otp_email
import os
User = get_user_model()

# 🔹 Serializer per il profilo utente (visibile da admin o API backend)
class UserProfileSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, required=False)

    class Meta:
        model = User
        fields = [
            'id',
            'email',
            'first_name',
            'last_name',
            'phone_number',
            'date_of_birth',
            'gender',
            'country',
            'city',
            'address',
            'zip_code',
            'document_id',
            'notify_email',
            'notify_whatsapp',
            'notify_push',
            'accepted_terms',
            'accepted_privacy',
            'is_active',
            'is_staff',
            'created_at',
            'updated_at',
            'password',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'is_staff']

    def create(self, validated_data):
        password = validated_data.pop('password', None)
        user = User(**validated_data)
        if password:
            user.set_password(password)
        else:
            raise serializers.ValidationError({"password": "La password è obbligatoria."})
        user.save()
        return user

    def update(self, instance, validated_data):
        password = validated_data.pop('password', None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        if password:
            instance.set_password(password)
        instance.save()
        return instance

# 🔹 Serializer per registrazione pubblica da sito o app
class UserRegistrationSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True)

    class Meta:
        model = User
        fields = [
            'id',
            'email',
            'password',
            'first_name',
            'last_name',
            'accepted_terms',
            'accepted_privacy'
        ]

    def validate(self, attrs):
        if not attrs.get('accepted_terms'):
            raise serializers.ValidationError({"accepted_terms": "Devi accettare i termini e condizioni."})
        if not attrs.get('accepted_privacy'):
            raise serializers.ValidationError({"accepted_privacy": "Devi accettare la privacy policy."})
        return attrs

    def create(self, validated_data):
        password = validated_data.pop('password')
        user = User(**validated_data)
        user.set_password(password)
        user.is_active = False  # Utente inattivo finché non verifica OTP
        user.save()

        # Genera OTP e salva su utente
        user.generate_otp()
        invia_otp_email(user)
        return user
#invia email otp
class OTPVerificationSerializer(serializers.Serializer):
    email = serializers.EmailField()
    otp_code = serializers.CharField(max_length=6)

    def validate(self, attrs):
        try:
            user = User.objects.get(email=attrs['email'])
        except User.DoesNotExist:
            raise serializers.ValidationError("Utente non trovato.")

        if not user.is_otp_valid(attrs['otp_code']):
            raise serializers.ValidationError("OTP non valido o scaduto.")

        # Attiva l'account e pulisce OTP
        user.is_active = True
        user.otp_code = None
        user.otp_created_at = None
        user.save()

        return {"detail": "Registrazione confermata con successo."}

# 🔹 Serializer Recensione
class RecensioneSerializer(serializers.ModelSerializer):
    class Meta:
        model = Recensione
        fields = '__all__'

# 🔹 Serializer Artista
class ArtistaSerializer(serializers.ModelSerializer):
    class Meta:
        model = Artista
        fields = '__all__'

# 🔹 Serializer Luoghi
class LuoghiSerializer(serializers.ModelSerializer):
    class Meta:
        model = Luoghi
        fields = '__all__'

# 🔹 Serializer Categoria
class CategoriaSerializer(serializers.ModelSerializer):
    class Meta:
        model = Categoria
        fields = '__all__'

# 🔹 Serializer Piattaforme (es. TicketOne)
class PiattaformaSerializer(serializers.ModelSerializer):
    class Meta:
        model = Piattaforma
        fields = '__all__'

# 🔹 Serializer relazioni evento-piattaforma
class EventoPiattaformaSerializer(serializers.ModelSerializer):
    piattaforma = PiattaformaSerializer(read_only=True)

    class Meta:
        model = EventoPiattaforma
        fields = '__all__'

# 🔹 Serializer evento
class EventoSerializer(serializers.ModelSerializer):
    piattaforme_collegate = EventoPiattaformaSerializer(many=True, read_only=True)
    luogo = LuoghiSerializer(read_only=True)
    categoria = CategoriaSerializer(read_only=True)
    artista_principale = ArtistaSerializer(read_only=True)

    class Meta:
        model = Evento
        fields = '__all__'

# 🔹 Serializer Sconti
class ScontiSerializer(serializers.ModelSerializer):
    class Meta:
        model = Sconti
        fields = '__all__'

# 🔹 Serializer Abbonamento
class AbbonamentoSerializer(serializers.ModelSerializer):
    class Meta:
        model = Abbonamento
        fields = '__all__'

# 🔹 Serializer Monitoraggio
class MonitoraggioSerializer(serializers.ModelSerializer):
    class Meta:
        model = Monitoraggio
        fields = '__all__'

# 🔹 Serializer Notifica
class NotificaSerializer(serializers.ModelSerializer):
    class Meta:
        model = Notifica
        fields = '__all__'

# 🔹 Serializer Biglietto
class BigliettoUploadSerializer(serializers.ModelSerializer):
    path_file = serializers.FileField(max_length=None, allow_empty_file=False)

    class Meta:
        model = Biglietto
        fields = '__all__'
        extra_kwargs = {
            'path_file': {'required': False, 'allow_null': False}
        }

    def validate_path_file(self,file):
        max_size = 2 * 1024 * 1024 #max 2 MB
        if file.size > max_size:
            raise serializers.ValidationError("File troppo grande (max 2 MB)")

        ext = os.path.splitext(file.name)[1].lower()
        if ext != '.pdf':
            raise serializers.ValidationError("Il file deve essere un PDF")

        return file

    def update(self, instance, validated_data):
        if 'path_file' not in validated_data:
            validated_data['path_file'] = instance.path_file
        return super().update(instance, validated_data)

    def to_representation(self, instance):
        rep = super().to_representation(instance)
        request= self.context.get('request')
        if instance.path_file and request:
            rep['path_file'] = request.build_absolute_uri(instance.path_file.url)
        return rep

class ShortUserProfileSerializer(serializers.Serializer):
    class Meta:
        model = User
        fields = ['first_name','last_name']

# 🔹 Serializer Rivendita
class RivenditaSerializer(serializers.ModelSerializer):
    class Meta:
        model = Rivendita
        fields = '__all__'

# 🔹 Serializer Acquisto
class AcquistoSerializer(serializers.ModelSerializer):
    class Meta:
        model = Acquisto
        fields = '__all__'

class EventoSerializer(serializers.ModelSerializer):
    piattaforme_collegate = EventoPiattaformaSerializer(many=True, read_only=True)
    luogo = LuoghiSerializer(read_only=True)
    categoria = CategoriaSerializer(read_only=True)
    artista_principale = ArtistaSerializer(read_only=True)

    class Meta:
        model = Evento
        fields = '__all__'