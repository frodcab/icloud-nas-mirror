> **Sin relación, aval ni patrocinio de Apple Inc. iCloud es una marca de Apple Inc.** Este proyecto usa una interfaz no documentada y puede dejar de funcionar en cualquier momento; úsalo bajo tu propia responsabilidad.

[Read in English](README.md)

# icloud-nas-mirror

**Sincronización bidireccional autoalojada entre iCloud Drive / iCloud Fotos y una carpeta de tu NAS o servidor**, con panel web, acceso con 2FA y límites de seguridad. Un único contenedor Docker, `amd64` y `arm64`.

> **Estado: beta.** Lleva semanas funcionando con la cuenta del autor, pero depende de una interfaz que Apple no documenta ni soporta. Lee los [riesgos](#riesgos-léelos-primero) antes de usarlo.
> El panel web y los correos están en **español e inglés**: se cambia con **ES · EN** en la parte de arriba del panel (por defecto sigue el idioma del navegador) y `NOTIFY_LANG` fija el de los correos. Los logs están en inglés. Los nombres de las carpetas y ficheros que la app crea en el disco (`Álbumes/`, `Oculto/`, `Subir/`, `(editada)`, `(conflicto NAS …)`) se eligen con `NAMES_LANG` (`es` o `en`): elígelo **antes de la primera sincronización**, porque esos nombres se guardan en las bases de datos de estado.

## Qué hace

**iCloud Drive ⇄ una carpeta** (en los dos sentidos)
- Los cambios de un lado se llevan al otro. Si hay conflicto se guardan las dos copias (`nombre (conflicto NAS AAAA-MM-DD HHMM).ext`).
- No se pierde nada en silencio: los borrados pasan por una papelera (`data/trash/`, 30 días por defecto) y por «Eliminados recientemente» de iCloud.
- Si una sync fuera a borrar más de `CONFIRM_DELETES_OVER` ficheros, esos borrados **esperan tu confirmación** en el panel.
- Opcional: un `foto.jpg` junto a cada `foto.heic` (alta calidad, conserva EXIF/GPS/perfil de color; el HEIC no se toca).
- Las apps (Pages, Numbers, Keynote, Atajos…) se copian de iCloud al disco, solo en ese sentido.

**iCloud Fotos ⇄ `Photos/iCloud/AAAA/MM/`** (en los dos sentidos)
- Originales, versiones editadas (`… (editada)`), vídeos de Live Photo y tus álbumes (como enlaces duros en `Álbumes/<álbum>/`; `Albums/` con `NAMES_LANG=en`).
- Las fotos ocultas pueden ir a su propia carpeta, fuera de la biblioteca de tu gestor de fotos.
- Lo que dejes en `Subir/` (`Upload/` con `NAMES_LANG=en`) se sube a iCloud Fotos. Borrar una foto en el disco la borra en iCloud (con confirmación por encima del límite).
- Pensado para cuidar tu cuenta: límites por vuelta (descargas, GB, subidas, borrados), pausas entre transferencias, espera automática cuando Apple frena, y muestreo aleatorio para que unos pocos ficheros lentos no bloqueen al resto.

**Funcionamiento**
- Panel web: estado, progreso en vivo, actividad, conflictos, historial, borrados pendientes, almacenamiento y dispositivos. Pensado también para el móvil.
- Acceso con contraseña + TOTP (app de autenticación) y códigos de recuperación; bloqueo progresivo por IP. Ver [Seguridad](#seguridad).
- Vigilante: una vuelta que deja de avanzar se para, recibes un correo y el proceso se reinicia si no responde.
- Avisos opcionales por correo (fallo, recuperación, 2FA pendiente, conflictos).

## Inicio rápido

Necesitas Docker con Compose, una carpeta para los datos y un Apple ID.

```bash
git clone https://github.com/frodcab/icloud-nas-mirror.git
cd icloud-nas-mirror
cp .env.example .env            # edita ICLOUD_USERNAME, PUID/PGID, las carpetas y TZ
mkdir -p data drive home
printf 'ICLOUD_PASSWORD=tu-contraseña-de-apple\nGUI_PASS=una-contraseña-larga-para-el-panel\n' > data/secrets.env
chmod 600 data/secrets.env
docker compose up -d
```

Abre `http://<equipo>:8096`, entra como `admin` con la `GUI_PASS` que elegiste y configura tu app de autenticación. Apple enviará un código de verificación a tus dispositivos: introdúcelo en el panel. Hasta entonces la sync queda en pausa.

**Primer arranque, recomendado**
1. Deja `DRY_RUN=true` y `PHOTOS_DRY_RUN=true`. Los logs y el panel enseñan lo que *haría*.
2. Prueba primero con una carpeta pequeña: `SYNC_FOLDERS=Alguna/Carpeta/De/Prueba`.
3. Cuando todo cuadre, pon ambas a `false` y reinicia: `docker compose up -d`.

La imagen se publica en `ghcr.io/frodcab/icloud-nas-mirror`. Etiquetas: la versión (p. ej. `2026.11.07.01`) y `latest`.

## Cómo decide Drive qué hacer

| En el disco | En iCloud | Acción |
|---|---|---|
| sin cambios | cambiado o nuevo | descarga |
| cambiado o nuevo | sin cambios | sube |
| cambiado | cambiado | conflicto: se guardan las dos |
| borrado | sin cambios | borra en iCloud (30 días en «Eliminados recientemente») |
| sin cambios | borrado | lo mueve a la papelera local |

Salvaguardas: un lado vacío que borraría el otro (volumen sin montar, respuesta rota de Apple) aborta la sync; la primera sync nunca borra; los errores sueltos se reintentan en la siguiente.

## Configuración

Todo son variables de entorno; mira [`.env.example`](.env.example) y [`compose.yml`](compose.yml). Las importantes:

| Variable | Por defecto | Qué es |
|---|---|---|
| `ICLOUD_USERNAME` | obligatoria | Apple ID |
| `ICLOUD_PASSWORD`, `GUI_PASS`, `SMTP_PASSWORD` | en `data/secrets.env` | secretos, nunca en el compose |
| `DRY_RUN` / `PHOTOS_DRY_RUN` | `true` en el compose | solo registra lo que haría |
| `SYNC_FOLDERS` | vacío = todo | carpetas de iCloud Drive a sincronizar, separadas por comas |
| `SYNC_INTERVAL_MIN` / `PHOTOS_INTERVAL_MIN` | 45 / 240 | minutos entre vueltas |
| `CONFIRM_DELETES_OVER` | 5 | más borrados que esto esperan confirmación |
| `PHOTOS_MAX_DOWNLOADS`, `PHOTOS_MAX_GB`, … | 1000, 10 | límites por vuelta de Fotos |
| `PHOTOS_HIDDEN`, `PHOTOS_HIDDEN_DIR` | true, vacío | bajar las ocultas, y a dónde |
| `HEIC_TO_JPG` | true | JPG junto a cada HEIC |
| `AUTH_ALLOW_NETWORKS` | `192.168.0.0/16` | redes que nunca se bloquean |
| `PUID` / `PGID` | 1000 / 1000 | dueño de los ficheros que se escriben |

## Seguridad

- El panel tiene su propio acceso: contraseña (argon2id) + TOTP con anti-reutilización, 10 códigos de recuperación, cookies de sesión `HttpOnly`/`SameSite=Strict`, comprobaciones anti-CSRF y bloqueo progresivo por IP.
- `X-Forwarded-For` solo se acepta de la puerta de enlace del contenedor. Si lo expones fuera de tu red local, ponlo detrás de un proxy inverso con HTTPS.
- Tu contraseña de Apple vive solo en `data/secrets.env` (permisos 600) y en memoria. Las cookies de sesión de Apple se guardan en `data/session/`. **Protege la carpeta `data/` como un gestor de contraseñas.**
- Usa una `GUI_PASS` larga y única; luego puedes cambiarla desde el panel.
- **Funcionar sin el acceso.** Si ya proteges el panel (una VPN como Tailscale/WireGuard, o un proxy inverso con su propia autenticación como
  Authelia/Authentik), pon `AUTH_DISABLED=true`. Entonces no hay usuario, contraseña ni TOTP, ni página *Cuenta*. Viene **apagado por defecto** y es una
  decisión con riesgo real: cualquiera que llegue al puerto puede confirmar borrados, meter tu código 2FA de Apple o hacer sonar tus dispositivos.
  Siguen activas estas salvaguardas: solo se aceptan redes privadas (`10/8`, `172.16/12`, `192.168/16`, loopback, `100.64/10`) salvo que fijes
  `AUTH_ONLY_NETWORKS` (o `AUTH_ALLOW_PUBLIC=true`, bajo tu responsabilidad); el anti-CSRF; y la cabecera `Host` debe ser una IP, un nombre local
  (`nas`, `*.local`, `*.lan`) o uno que pongas en `AUTH_ALLOWED_HOSTS`/`PUBLIC_URL` (protección contra DNS rebinding). Detrás de un proxy que no esté
  en `AUTH_TRUSTED_PROXIES` la app solo ve la dirección del proxy, así que la comprobación de red no te protege: hazlo en el proxy.
- `/health` es público y no devuelve datos personales (versión, estado de la sesión, estado de la última vuelta).
- Avisa de las vulnerabilidades como se explica en [SECURITY.md](SECURITY.md).

## Riesgos (léelos primero)

- **No oficial.** No existe una API pública de iCloud. Este proyecto habla con la misma interfaz web que icloud.com, a través de [pyicloud](https://github.com/timlaing/pyicloud). Apple puede cambiarla sin avisar y romper la app, y sus condiciones de uso no autorizan este tipo de acceso. **Lo usas bajo tu responsabilidad; Apple podría restringir una cuenta que se comporte de forma inusual.** Para eso existen los límites y las esperas: no los subas a ciegas.
- **Bidireccional significa que los borrados se propagan.** Prueba antes con `DRY_RUN=true` y una carpeta pequeña, mantén tus propias copias de seguridad y lee con atención los avisos de borrados pendientes.
- Las cuentas con *Protección avanzada de datos* deben activar en el iPhone *Acceder a datos de iCloud en la web*. Las llaves de seguridad físicas (FIDO2) no están soportadas.
- Sin garantía de ningún tipo (ver la licencia).

## Actualizar

```bash
docker compose pull && docker compose up -d
```

Haz copia de `data/` (sesiones, estado, usuarios). Perderla solo obliga a volver a iniciar sesión con Apple; tus ficheros están en el disco y en iCloud.

## Problemas frecuentes

- *Se queda esperando el 2FA*: abre el panel e introduce el código que envió Apple. Las sesiones caducan cada pocas semanas.
- *Apple rechaza la contraseña*: la app **no** reintenta sola (los fallos repetidos pueden bloquear la cuenta). Corrige `data/secrets.env` y pulsa «Reconectar» en el panel.
- *Errores de permisos en las carpetas*: pon `PUID`/`PGID` del dueño de `drive/` y `home/`.
- *La primera vuelta de Fotos es lenta*: está limitada a propósito (1000 ficheros / 10 GB por vuelta). Una biblioteca de 200 GB tarda un par de días.
- Logs: `docker compose logs -f`. Al informar de un problema, **quita antes correos, rutas con nombres personales y cualquier código o token**.

## Contribuciones

Se agradecen las incidencias y las sugerencias. Por ahora **no se aceptan pull requests** (el proyecto es pequeño y cambiar los términos de la licencia con muchos autores es difícil); puede cambiar.

## Hoja de ruta

- Más idiomas de interfaz (un idioma es un fichero `static/i18n-<código>.json`; las traducciones se agradecen como incidencias).
- Capturas y un vídeo corto en este README.
- Más pruebas con cuentas reales antes de cada versión.

## Licencia

Copyright (C) 2026 F. Javier R. Cabero

Publicado bajo la **GNU Affero General Public License v3.0 solo** ([LICENSE](LICENSE)). Si modificas este software y dejas que otros lo usen por red, debes ofrecerles el código fuente de tu versión; por eso el pie del panel enlaza a este repositorio. El software de terceros y sus licencias están en [NOTICE](NOTICE).
