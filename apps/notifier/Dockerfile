FROM golang:1.26.4-alpine AS builder
WORKDIR /app
COPY apps/notifier/go.mod apps/notifier/go.sum ./
RUN go mod download
COPY apps/notifier/ .
RUN CGO_ENABLED=0 GOOS=linux go build -o /notifier ./cmd/notifier

FROM alpine:3.21
RUN apk add --no-cache ca-certificates
COPY --from=builder /notifier /notifier
ENTRYPOINT ["/notifier"]
