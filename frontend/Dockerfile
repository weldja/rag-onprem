# Builds the static frontend and serves it from nginx, which also proxies /api.
# npm runs only at build time; the running container serves files and nothing else.
FROM node:22.11.0-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
RUN npm run build

FROM nginx:1.27-alpine
COPY --from=build /app/dist /usr/share/nginx/html
