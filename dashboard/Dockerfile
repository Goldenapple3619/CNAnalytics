FROM node:22-alpine AS stamp

WORKDIR /client

COPY package.json package-lock.json ./
RUN npm ci

COPY index.html ./
COPY robots.txt ./
COPY sitemap.xml ./
COPY script script
COPY style style
COPY tools tools

RUN npm run build:prod

RUN node tools/stamp.mjs /client


FROM nginxinc/nginx-unprivileged:1.27-alpine

COPY nginx/default.conf /etc/nginx/conf.d/default.conf
COPY nginx/security-headers.conf /etc/nginx/snippets/security-headers.conf

COPY assets /usr/share/nginx/html/assets

COPY --from=stamp /client/index.html /usr/share/nginx/html/
COPY --from=stamp /client/robots.txt /usr/share/nginx/html/
COPY --from=stamp /client/sitemap.xml /usr/share/nginx/html/
COPY --from=stamp /client/script /usr/share/nginx/html/script
COPY --from=stamp /client/style /usr/share/nginx/html/style

EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=3s --retries=3 CMD wget -qO- http://127.0.0.1:8080/healthz || exit 1
