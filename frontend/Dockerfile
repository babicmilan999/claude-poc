# One image definition for both apps; APP picks the Angular project to build.
#   docker build --build-arg APP=customer-app .   (or APP=ops-console)
# infra/docker-compose.yml passes APP and maps host 4300/4200 to port 80 here.
#
# API_MODE (build arg): `real` (default) builds against the real agent service
# (`--configuration real`); `mock` builds the in-browser mock (`--configuration production`).
# At run time, API_BASE_URL (and CUSTOMER_APP_URL for the ops console) override the
# agent service URL without rebuilding: see docker/write-config.sh and README.md.

# --- build -----------------------------------------------------------------------
FROM node:22-alpine AS build
ARG APP
ARG API_MODE=real
RUN test "$APP" = "customer-app" || test "$APP" = "ops-console" \
    || (echo "APP must be customer-app or ops-console, got '$APP'" >&2; exit 1)
RUN test "$API_MODE" = "real" || test "$API_MODE" = "mock" \
    || (echo "API_MODE must be real or mock, got '$API_MODE'" >&2; exit 1)
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
RUN if [ "$API_MODE" = "real" ]; then CONFIG=real; else CONFIG=production; fi \
    && npx ng build "$APP" --configuration "$CONFIG" \
    && mkdir -p /out \
    && cp -r "dist/$APP/browser/." /out/

# --- serve -----------------------------------------------------------------------
FROM nginx:1.27-alpine
COPY --from=build /out /usr/share/nginx/html
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY docker/write-config.sh /docker-entrypoint.d/40-write-config.sh
RUN chmod +x /docker-entrypoint.d/40-write-config.sh
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
