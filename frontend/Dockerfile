# DB Buddy web UI.
#
# The app is server-rendered (TanStack Start), so the build emits dist/server as
# well as dist/client — there is no index.html to hand to a static server. It is
# served with `vite preview`, which runs that SSR build.
#
# Dev dependencies stay in the image because `vite preview` needs them; this is a
# demo image, sized for one command rather than for a CDN.

FROM node:20-slim

ENV NODE_ENV=development
WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY . .

# Baked in at build time: Vite inlines import.meta.env at compile, so this cannot
# be a runtime environment variable. It is the URL the *browser* uses to reach
# the API, which is why it is localhost and not the compose service name.
ARG VITE_API_BASE=http://localhost:8000
ENV VITE_API_BASE=${VITE_API_BASE}
RUN npm run build

EXPOSE 3000

CMD ["npx", "vite", "preview", "--host", "0.0.0.0", "--port", "3000"]
