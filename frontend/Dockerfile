# One image definition for both apps; APP picks the Angular project to build.
#   docker build --build-arg APP=customer-app .   (or APP=ops-console)
# infra/docker-compose.yml passes APP and maps host 4300/4200 to port 80 here.

# --- build -----------------------------------------------------------------------
FROM node:22-alpine AS build
ARG APP
RUN test "$APP" = "customer-app" || test "$APP" = "ops-console" \
    || (echo "APP must be customer-app or ops-console, got '$APP'" >&2; exit 1)
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
RUN npx ng build "$APP" \
    && mkdir -p /out \
    && cp -r "dist/$APP/browser/." /out/

# --- serve -----------------------------------------------------------------------
FROM nginx:1.27-alpine
COPY --from=build /out /usr/share/nginx/html
RUN printf '%s\n' \
    'server {' \
    '  listen 80;' \
    '  server_name _;' \
    '  root /usr/share/nginx/html;' \
    '  index index.html;' \
    '  location = /index.html { add_header Cache-Control "no-cache"; }' \
    '  location / { try_files $uri $uri/ /index.html; }' \
    '}' > /etc/nginx/conf.d/default.conf
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
