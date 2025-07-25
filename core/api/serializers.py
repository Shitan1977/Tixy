from rest_framework import serializers
from django.contrib.auth import get_user_model
from .models import Evento, Piattaforma, EventoPiattaforma, Biglietto

User = get_user_model()

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
        read_only_fields = ['id', 'created_at', 'updated_at']

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
        user.save()
        return user

# registrazione pubblica
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
        user.save()
        return user


#parte relativa agli eventi

class PiattaformaSerializer(serializers.ModelSerializer):
    class Meta:
        model = Piattaforma
        fields = ['id', 'nome', 'url_base']


class EventoPiattaformaSerializer(serializers.ModelSerializer):
    piattaforma = PiattaformaSerializer(read_only=True)

    class Meta:
        model = EventoPiattaforma
        fields = [
            'id',
            'piattaforma',
            'url_pagina_evento',
            'disponibilità_biglietti',
            'prezzo_minimo',
            'timestamp_aggiornamento'
        ]


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
            'città',
            'url_immagine',
            'categoria',
            'stato_disponibilità',
            'attivo',
            'timestamp_aggiornamento',
            'piattaforme_collegate',
        ]

# Parte dei File

class FileUpload(serializers.ModelSerializer):
    class Meta:
        model = Biglietto
        fields = '__all__'
        read_only_fields = ['id','tipo_biglietto','data_caricamento','is_valid','path_file']