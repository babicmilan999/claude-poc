# Banking service image. Build context: banking-service/.
FROM eclipse-temurin:21-jdk AS build
WORKDIR /src
COPY .mvn .mvn
COPY mvnw pom.xml ./
RUN ./mvnw -B -q dependency:go-offline
COPY src src
# Tests run in CI (make test-banking); they need ../contracts, which is outside this context.
RUN ./mvnw -B -q package -DskipTests

FROM eclipse-temurin:21-jre
WORKDIR /app
COPY --from=build /src/target/banking-service-*.jar app.jar
EXPOSE 8080
# Health: GET /actuator/health
ENTRYPOINT ["java", "-jar", "/app/app.jar"]
