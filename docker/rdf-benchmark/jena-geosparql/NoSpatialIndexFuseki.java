import org.apache.jena.fuseki.main.FusekiServer;
import org.apache.jena.geosparql.configuration.GeoSPARQLConfig;
import org.apache.jena.query.Dataset;
import org.apache.jena.tdb2.TDB2Factory;

public final class NoSpatialIndexFuseki {
    private NoSpatialIndexFuseki() {
    }

    public static void main(String[] args) {
        String datasetName = "yago2geo";
        String tdbPath = "/fuseki/databases/yago2geo";
        int port = 3030;
        boolean loopback = false;

        for (int i = 0; i < args.length; i++) {
            switch (args[i]) {
                case "--dataset":
                case "-d":
                    datasetName = args[++i];
                    break;
                case "--tdb":
                case "-t":
                    tdbPath = args[++i];
                    break;
                case "--port":
                case "-p":
                    port = Integer.parseInt(args[++i]);
                    break;
                case "--loopback":
                case "-l":
                    loopback = Boolean.parseBoolean(args[++i]);
                    break;
                default:
                    if (args[i].startsWith("--dataset=")) {
                        datasetName = args[i].substring("--dataset=".length());
                    } else if (args[i].startsWith("--tdb=")) {
                        tdbPath = args[i].substring("--tdb=".length());
                    } else if (args[i].startsWith("--port=")) {
                        port = Integer.parseInt(args[i].substring("--port=".length()));
                    } else if (args[i].startsWith("--loopback=")) {
                        loopback = Boolean.parseBoolean(args[i].substring("--loopback=".length()));
                    }
                    break;
            }
        }

        GeoSPARQLConfig.setupNoIndex(Boolean.TRUE);
        GeoSPARQLConfig.allowGeometrySRSTransformation(true);

        Dataset dataset = TDB2Factory.connectDataset(tdbPath);
        String endpoint = datasetName.startsWith("/") ? datasetName : "/" + datasetName;
        FusekiServer server = FusekiServer.create()
                .port(port)
                .loopback(loopback)
                .add(endpoint, dataset, false)
                .build();

        server.start();
        System.out.printf("NoSpatialIndexFuseki endpoint: http://0.0.0.0:%d%s/sparql%n", port, endpoint);
        server.join();
    }
}
