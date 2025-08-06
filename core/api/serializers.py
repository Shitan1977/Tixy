from rest_framework import serializers
from django.contrib.auth import get_user_model
from .models import Evento, Piattaforma, EventoPiattaforma, Biglietto
from .utils import invia_otp_email
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

# 🔹 Serializer piattaforme (es. TicketOne)
class PiattaformaSerializer(serializers.ModelSerializer):
    class Meta:
        model = Piattaforma
        fields = ['id', 'nome', 'url_base']

# 🔹 Serializer relazioni evento-piattaforma
class EventoPiattaformaSerializer(serializers.ModelSerializer):
    piattaforma = PiattaformaSerializer(read_only=True)

    class Meta:
        model = EventoPiattaforma
        fields = [
            'id',
            'piattaforma',
            'url_pagina_evento',
            'disponibilita_biglietti',
            'prezzo_minimo',
            'timestamp_aggiornamento'
        ]

# 🔹 Serializer evento
class EventoSerializer(serializers.ModelSerializer):
    piattaforme_collegate = EventoPiattaformaSerializer(many=True, read_only=True)

    class Meta:
        model = Evento
        fields = [
            'id',
            'nome_evento',
            'descrizione',
            'artista',
            'data_ora',
            'luogo',
            'citta',
            'url_immagine',
            'categoria',
            'stato_disponibilita',
            'attivo',
            'timestamp_aggiornamento',
            'piattaforme_collegate',
        ]

# 🔹 Serializer caricamento biglietto (PDF)
class BigliettoUploadSerializer(serializers.ModelSerializer):
    class Meta:
        model = Biglietto
        fields = [
            'id',
            'tipo_biglietto',
            'titolo',
            'path_file',
            'data_caricamento',
            'is_valid'
        ]
        read_only_fields = ['id', 'data_caricamento', 'is_valid', 'tipo_biglietto']
