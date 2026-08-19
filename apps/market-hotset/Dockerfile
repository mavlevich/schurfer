FROM golang:1.26.6-alpine AS builder
WORKDIR /app
COPY apps/market-hotset/go.mod apps/market-hotset/go.sum ./
RUN go mod download
COPY apps/market-hotset/ .
RUN CGO_ENABLED=0 GOOS=linux go build -o /market-hotset ./cmd/market-hotset

FROM alpine:3.21
RUN apk add --no-cache ca-certificates
COPY --from=builder /market-hotset /market-hotset
ENTRYPOINT ["/market-hotset"]
