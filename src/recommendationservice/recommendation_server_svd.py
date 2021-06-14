#!/usr/bin/python
#
# Copyright 2018 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Copyright(c) Microsoft Corporation. All rights reserved.

# Licensed under the MIT License.


import os
import time
import traceback
from concurrent import futures

import googleclouddebugger
import googlecloudprofiler
import surprise
from google.auth.exceptions import DefaultCredentialsError
import grpc
from opencensus.ext.stackdriver import trace_exporter as stackdriver_exporter
from opencensus.ext.grpc import server_interceptor
from opencensus.trace import samplers
from opencensus.common.transports.async_ import AsyncTransport

import demo_pb2
import demo_pb2_grpc
from grpc_health.v1 import health_pb2
from grpc_health.v1 import health_pb2_grpc

import logging
import numpy as np
import hashlib

from reco_utils.common.timer import Timer
from reco_utils.dataset import movielens
from reco_utils.dataset.python_splitters import python_stratified_split
from reco_utils.recommender.surprise.surprise_utils import compute_ranking_predictions

from logger import getJSONLogger

# top k items to recommend
TOP_K = 10

# Select MovieLens data size: 100k, 1m, 10m, or 20m
MOVIELENS_DATA_SIZE = '100k'

data = movielens.load_pandas_df(
    size=MOVIELENS_DATA_SIZE,
    header=["userID", "itemID", "rating"]
)


# Convert the float precision to 32-bit in order to reduce memory consumption
data['rating'] = data['rating'].astype(np.float32)

train, test = python_stratified_split(
    data, ratio=0.75, col_user='userID', col_item='itemID', seed=42)

train_users = len(train['userID'].unique())


# 'reader' is being used to get rating scale (for MovieLens, the scale is [1, 5]).
# 'rating_scale' parameter can be used instead for the later version of surprise lib:
# https://github.com/NicolasHug/Surprise/blob/master/surprise/dataset.py
train_set = surprise.Dataset.load_from_df(
    train, reader=surprise.Reader('ml-100k')).build_full_trainset()


logging.basicConfig(level=logging.DEBUG,
                    format='%(asctime)s %(levelname)-8s %(message)s')

model = surprise.SVD(random_state=0, n_factors=200, n_epochs=30, verbose=True)

with Timer() as train_time:
    model.fit(train_set)

print("Took {} seconds for training.".format(train_time.interval))

logger = getJSONLogger('recommendationservice-server')


def initStackdriverProfiling():
    project_id = None
    try:
        project_id = os.environ["GCP_PROJECT_ID"]
    except KeyError:
        # Environment variable not set
        pass

    for retry in range(1, 4):
        try:
            if project_id:
                googlecloudprofiler.start(
                    service='recommendation_server', service_version='1.0.0', verbose=0, project_id=project_id)
            else:
                googlecloudprofiler.start(
                    service='recommendation_server', service_version='1.0.0', verbose=0)
            logger.info("Successfully started Stackdriver Profiler.")
            return
        except (BaseException) as exc:
            logger.info(
                "Unable to start Stackdriver Profiler Python agent. " + str(exc))
            if (retry < 4):
                logger.info(
                    "Sleeping %d seconds to retry Stackdriver Profiler agent initialization" % (retry*10))
                time.sleep(1)
            else:
                logger.warning(
                    "Could not initialize Stackdriver Profiler after retrying, giving up")
    return


class RecommendationService(demo_pb2_grpc.RecommendationServiceServicer):
    def ListRecommendations(self, request, context):
        # max_responses = 5
        # fetch list of products from product catalog stub
        # cat_response = product_catalog_stub.ListProducts(demo_pb2.Empty())
        # product_ids = [x.id for x in cat_response.products]
        # filtered_products = list(set(product_ids)-set(request.product_ids))
        # num_products = len(filtered_products)
        # num_return = min(max_responses, num_products)
        # sample list of indicies to return
        user_id = 1 + int(hashlib.sha1(request.user_id.encode("utf-8")
                                       ).hexdigest(), 16) % train_users

        predictions = compute_ranking_predictions(
            model, test[test['userID'] == user_id], usercol='userID', itemcol='itemID')
        # logger.info(predictions)
        # logger.info(prediction.head())
        # indices = random.sample(range(num_products), num_return)
        # fetch product ids from indices
        # prod_list = [filtered_products[i] for i in indices]
        prod_list = ['1YMWWN1N4O', 'L9ECAV7KIM',
                     'LS4PSXUNUM', '9SIQT8TOJO', 'OLJCESPC7Z']
        logger.info(
            "[Recv ListRecommendations] product_ids={}".format(prod_list))
        # build and return response
        response = demo_pb2.ListRecommendationsResponse()
        response.product_ids.extend(prod_list)
        return response

    def Check(self, request, context):
        return health_pb2.HealthCheckResponse(
            status=health_pb2.HealthCheckResponse.SERVING)

    def Watch(self, request, context):
        return health_pb2.HealthCheckResponse(
            status=health_pb2.HealthCheckResponse.UNIMPLEMENTED)


if __name__ == "__main__":
    logger.info("initializing recommendationservice")

    try:
        if "DISABLE_PROFILER" in os.environ:
            raise KeyError()
        else:
            logger.info("Profiler enabled.")
            initStackdriverProfiling()
    except KeyError:
        logger.info("Profiler disabled.")

    try:
        if "DISABLE_TRACING" in os.environ:
            raise KeyError()
        else:
            logger.info("Tracing enabled.")
            sampler = samplers.AlwaysOnSampler()
            exporter = stackdriver_exporter.StackdriverExporter(
                project_id=os.environ.get('GCP_PROJECT_ID'),
                transport=AsyncTransport)
            tracer_interceptor = server_interceptor.OpenCensusServerInterceptor(
                sampler, exporter)
    except (KeyError, DefaultCredentialsError):
        logger.info("Tracing disabled.")
        tracer_interceptor = server_interceptor.OpenCensusServerInterceptor()

    try:
        if "DISABLE_DEBUGGER" in os.environ:
            raise KeyError()
        else:
            logger.info("Debugger enabled.")
            try:
                googleclouddebugger.enable(
                    module='recommendationserver',
                    version='1.0.0'
                )
            except (Exception, DefaultCredentialsError):
                logger.error("Could not enable debugger")
                logger.error(traceback.print_exc())
                pass
    except (Exception, DefaultCredentialsError):
        logger.info("Debugger disabled.")

    port = os.environ.get('PORT', "8080")
    catalog_addr = os.environ.get('PRODUCT_CATALOG_SERVICE_ADDR', '')
    if catalog_addr == "":
        raise Exception(
            'PRODUCT_CATALOG_SERVICE_ADDR environment variable not set')
    logger.info("product catalog address: " + catalog_addr)
    channel = grpc.insecure_channel(catalog_addr)
    product_catalog_stub = demo_pb2_grpc.ProductCatalogServiceStub(channel)

    # create gRPC server
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10),
                         interceptors=(tracer_interceptor,))

    # add class to gRPC server
    service = RecommendationService()
    demo_pb2_grpc.add_RecommendationServiceServicer_to_server(service, server)
    health_pb2_grpc.add_HealthServicer_to_server(service, server)

    # start server
    logger.info("listening on port: " + port)
    server.add_insecure_port('[::]:'+port)
    server.start()

    # keep alive
    try:
        while True:
            time.sleep(10000)
    except KeyboardInterrupt:
        server.stop(0)