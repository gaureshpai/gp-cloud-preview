FROM busybox:1.36
RUN mkdir -p /www && printf 'gp-cloud smoke ok\n' > /www/index.html
EXPOSE 2222
CMD ["httpd", "-f", "-p", "2222", "-h", "/www"]
