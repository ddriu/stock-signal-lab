# Publicación gratuita con Streamlit Community Cloud

La aplicación está preparada para desplegarse desde un repositorio de GitHub.
El acceso se protege con cuentas independientes y hashes PBKDF2 guardados en los
secretos del alojamiento. El archivo real `.streamlit/secrets.toml` está excluido de Git.

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
[users.alberite]
display_name = "Alberite"
role = "admin"
password_hash = "HASH_PBKDF2_DEL_ADMIN"

[users.usuario1]
display_name = "Usuario 1"
role = "user"
password_hash = "HASH_PBKDF2_DISTINTO"

# Repite un bloque [users.nombre] por cada cuenta.

[deployment]
persistent_journal = false

[supabase]
url = "https://TU_PROYECTO.supabase.co"
secret_key = "sb_secret_TU_CLAVE_DE_SERVIDOR"
table = "operations"
```

El hash de una contraseña nueva se genera localmente con:

```bash
source .venv/bin/activate
python -c "from src.auth import hash_password; print(hash_password('TU_CONTRASEÑA'))"
```

No pegues la contraseña en GitHub y no subas el archivo local de secretos.

## 4. Activar el diario persistente gratuito

1. Crea un proyecto gratuito en Supabase.
2. Abre **SQL Editor**, pega `supabase/schema.sql` y ejecútalo.
3. Copia la **Project URL** y una clave secreta `sb_secret_*`.
4. Añádelas al bloque `[supabase]` de los Secrets de Streamlit.
5. Reinicia la aplicación.

La clave secreta evita las políticas RLS y por eso **nunca** debe publicarse en
GitHub ni copiarse en código cliente. La tabla no concede permisos a las claves
públicas. Cada fila queda asociada al usuario autenticado de la aplicación. Las
cuentas normales sólo construyen un diario para su propio nombre; el rol `admin`
dispone además de la vista agregada y del formulario para registrar operaciones
en nombre de los usuarios.

Sin `[supabase]`, la instalación local continúa usando SQLite. En Community Cloud,
`persistent_journal = false` mantiene el diario desactivado para evitar pérdidas.

## Seguridad

El acceso incluido admite cuentas y roles separados, adecuado para un grupo privado
pequeño. No incluye recuperación de contraseña, segundo factor ni bloqueo global de
intentos. Para una comunidad más amplia se recomienda sustituirlo por OIDC con
Google o Microsoft.
