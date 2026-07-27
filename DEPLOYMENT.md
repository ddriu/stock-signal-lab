# Publicación gratuita con Streamlit Community Cloud

La aplicación está preparada para desplegarse desde un repositorio de GitHub.
El acceso se protege con un usuario y un hash PBKDF2 guardados en los secretos
del alojamiento. El archivo real `.streamlit/secrets.toml` está excluido de Git.

## 1. Preparar GitHub

1. Crea un repositorio nuevo, preferiblemente privado.
2. Sube el contenido de esta carpeta respetando `.gitignore`.
3. Comprueba que **no** aparecen `.streamlit/secrets.toml`,
   `data/trading_journal.db` ni `.venv`.

## 2. Crear la aplicación

1. Entra en <https://share.streamlit.io>.
2. Conecta tu cuenta de GitHub.
3. Pulsa **Create app** y elige el repositorio.
4. Selecciona `app.py` como archivo principal.
5. Elige una dirección disponible terminada en `.streamlit.app`.

## 3. Configurar los secretos

En **Advanced settings > Secrets**, pega el contenido equivalente a:

```toml
[app_auth]
username = "TU_USUARIO"
password_hash = "TU_HASH_PBKDF2"

[deployment]
persistent_journal = false
```

El hash de una contraseña nueva se genera localmente con:

```bash
source .venv/bin/activate
python -c "from src.auth import hash_password; print(hash_password('TU_CONTRASEÑA'))"
```

No pegues la contraseña en GitHub y no subas el archivo local de secretos.

## 4. Limitación del diario

Streamlit Community Cloud puede reiniciar o reconstruir el contenedor. Por ese
motivo, `persistent_journal = false` desactiva el diario SQLite en la versión
web antes de que alguien confíe en datos que podrían desaparecer.

Para ofrecer carteras independientes y persistentes a varios amigos, la siguiente
fase es conectar una base PostgreSQL externa, por ejemplo Supabase, y asociar cada
operación a un identificador de usuario. El plan gratuito de Supabase es suficiente
para una aplicación personal pequeña, aunque el proyecto puede pausarse por inactividad.

## Seguridad

El acceso incluido es una contraseña compartida, adecuada para familia, amigos o
una demostración. No incluye recuperación de contraseña, segundo factor ni bloqueo
global de intentos. Para usuarios individuales se recomienda el inicio de sesión
OIDC de Streamlit con Google o Microsoft.
