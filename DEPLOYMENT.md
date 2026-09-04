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

[users.luci]
display_name = "Luci"
role = "user"
password_hash = "HASH_PBKDF2_DISTINTO"

# Repite un bloque [users.nombre] por cada cuenta.

[deployment]
persistent_journal = false

[supabase]
url = "https://TU_PROYECTO.supabase.co"
secret_key = "sb_secret_TU_CLAVE_DE_SERVIDOR"
table = "operations"
favorites_table = "favorites"
analysis_table = "analysis_snapshots"

[email]
smtp_host = "smtp.gmail.com"
smtp_port = 465
use_ssl = true
username = "CORREO_GMAIL_DEL_PROYECTO"
app_password = "CONTRASEÑA_DE_APLICACION_DE_GOOGLE"
sender_email = "CORREO_GMAIL_DEL_PROYECTO"
sender_name = "Stock Signal Lab"
```

El hash de una contraseña nueva se genera localmente con:

```bash
source .venv/bin/activate
python -c "from src.auth import hash_password; print(hash_password('TU_CONTRASEÑA'))"
```

No pegues la contraseña en GitHub y no subas el archivo local de secretos.

## 4. Activar el diario persistente gratuito

1. Crea un proyecto gratuito en Supabase.
2. Abre **SQL Editor**, pega `supabase/schema.sql` y ejecútalo. Es seguro volver
   a ejecutarlo al actualizar: añade las columnas nuevas sin borrar operaciones.
   Para la versión de estabilización también puedes ejecutar únicamente
   `supabase/migration_operation_reconciliation.sql`: añade cuenta de bróker,
   liquidación real en euros y las notas del último análisis enviado por correo.
3. Copia la **Project URL** y una clave secreta `sb_secret_*`.
4. Añádelas al bloque `[supabase]` de los Secrets de Streamlit.
5. Reinicia la aplicación.

La clave secreta evita las políticas RLS y por eso **nunca** debe publicarse en
GitHub ni copiarse en código cliente. La tabla no concede permisos a las claves
públicas. Cada cartera privada queda asociada al usuario autenticado. La cartera
compartida utiliza el propietario interno `grupo_compartido` y guarda además quién
registró cada movimiento. Los miembros sólo pueden eliminar desde la interfaz sus
propios movimientos compartidos; el rol `admin` puede corregir cualquiera, dispone
de la vista agregada y puede registrar operaciones en nombre de los usuarios.
El mismo esquema crea listas de favoritos privadas y del grupo y el historial privado
de análisis, sin borrar ni modificar las operaciones existentes.
También crea `email_alert_preferences` y `email_alert_states`: la primera conserva
el correo y las opciones privadas; la segunda recuerda el último estado de cada
empresa para no repetir el mismo aviso. Desde la versión de estabilización guarda
además las notas de crecimiento, fundamentales y oportunidad de esa misma revisión,
por lo que la tabla visible en la aplicación coincide con el correo diario.

Sin `[supabase]`, la instalación local continúa usando SQLite. En Community Cloud,
`persistent_journal = false` mantiene el diario desactivado para evitar pérdidas.

## 5. Activar las alertas gratuitas por Gmail

1. Crea una cuenta Gmail exclusiva para Stock Signal Lab y activa la verificación
   en dos pasos.
2. Genera una contraseña de aplicación de Google. No utilices ni compartas la
   contraseña habitual de la cuenta.
3. Añade el bloque `[email]` anterior a los secretos de Streamlit. Esto activa el
   botón de correo de prueba de la aplicación.
4. En GitHub abre **Settings > Secrets and variables > Actions** y crea:
   `SUPABASE_URL`, `SUPABASE_SECRET_KEY`, `EMAIL_SMTP_USERNAME` y
   `EMAIL_SMTP_PASSWORD`.
5. Abre **Actions > Enviar alertas diarias > Run workflow** para la primera prueba.
6. Cada usuario entra en **Más > Alertas por correo**, guarda su dirección y activa
   los tipos de aviso que quiera recibir.

El workflow programado se lanza a las 07:15 UTC de lunes a viernes. GitHub puede
retrasar algunos minutos las tareas gratuitas. Es un resumen con el último cierre
diario disponible, no una alerta intradía.

## Seguridad

El acceso incluido admite cuentas y roles separados, adecuado para un grupo privado
pequeño. No incluye recuperación de contraseña, segundo factor ni bloqueo global de
intentos. Para una comunidad más amplia se recomienda sustituirlo por OIDC con
Google o Microsoft.
