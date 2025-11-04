from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_apigateway as apigateway
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_neptune as neptune
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_notifications as s3n
from constructs import Construct

class ImageNameCapStackNeptune(Stack):
    """
    Neptune-enabled version of the stack for scaling to hundreds of people
    Use this when you need to scale beyond hardcoded relationships
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # S3 Bucket
        bucket = s3.Bucket(self, "Bucket",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            cors=[s3.CorsRule(
                allowed_methods=[s3.HttpMethods.GET, s3.HttpMethods.POST, s3.HttpMethods.PUT],
                allowed_origins=["*"],
                allowed_headers=["*"]
            )]
        )

        # DynamoDB Table
        table = dynamodb.Table(self, "Table",
            partition_key=dynamodb.Attribute(name="image_id", type=dynamodb.AttributeType.STRING),
            removal_policy=RemovalPolicy.DESTROY
        )
        
        # VPC for Neptune
        vpc = ec2.Vpc(self, "VPC",
            max_azs=2,
            nat_gateways=1
        )
        
        # Neptune Subnet Group
        subnet_group = neptune.CfnDBSubnetGroup(self, "NeptuneSubnetGroup",
            db_subnet_group_description="Subnet group for Neptune",
            subnet_ids=[subnet.subnet_id for subnet in vpc.private_subnets]
        )
        
        # Neptune Security Group
        neptune_sg = ec2.SecurityGroup(self, "NeptuneSG",
            vpc=vpc,
            description="Security group for Neptune cluster",
            allow_all_outbound=True
        )
        
        # Allow Lambda to connect to Neptune
        neptune_sg.add_ingress_rule(
            peer=ec2.Peer.ipv4(vpc.vpc_cidr_block),
            connection=ec2.Port.tcp(8182),
            description="Allow Gremlin connections from VPC"
        )
        
        # Neptune Cluster
        neptune_cluster = neptune.CfnDBCluster(self, "NeptuneCluster",
            db_subnet_group_name=subnet_group.ref,
            vpc_security_group_ids=[neptune_sg.security_group_id],
            deletion_protection=False,
            backup_retention_period=1,
            preferred_backup_window="03:00-04:00",
            preferred_maintenance_window="sun:04:00-sun:05:00"
        )
        
        # Neptune Instance
        neptune_instance = neptune.CfnDBInstance(self, "NeptuneInstance",
            db_instance_class="db.t3.medium",
            db_cluster_identifier=neptune_cluster.ref
        )

        # Lambda Role with Neptune permissions
        lambda_role = iam.Role(
            self,
            "Role",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                ),
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaVPCAccessExecutionRole"
                )
            ],
            inline_policies={
                "AllPermissions": iam.PolicyDocument(
                    statements=[
                        iam.PolicyStatement(
                            actions=["*"], resources=["*"]
                        )
                    ]
                )
            }
        )

        # Lambda Layer for Pillow
        pillow_layer = _lambda.LayerVersion(
            self,
            "PillowLayer",
            code=_lambda.Code.from_asset("lambda_layer"),
            compatible_runtimes=[_lambda.Runtime.PYTHON_3_11],
        )
        
        # Neptune Layer for gremlinpython
        neptune_layer = _lambda.LayerVersion(
            self,
            "NeptuneLayer",
            code=_lambda.Code.from_asset("neptune_layer"),
            compatible_runtimes=[_lambda.Runtime.PYTHON_3_11],
        )

        # Face Indexer Lambda
        face_indexer = _lambda.Function(self, "FaceIdx",
            runtime=_lambda.Runtime.PYTHON_3_11,
            handler="face_indexer.handler",
            code=_lambda.Code.from_asset("lambda"),
            role=lambda_role,
            timeout=Duration.seconds(60),
            environment={
                "BUCKET_NAME": bucket.bucket_name,
                "COLLECTION_ID": "faces"
            }
        )

        # Image Processor Lambda (with Neptune relationships)
        image_processor = _lambda.Function(
            self,
            "ImgProc",
            runtime=_lambda.Runtime.PYTHON_3_11,
            handler="image_processor.handler",
            code=_lambda.Code.from_asset("lambda"),
            role=lambda_role,
            timeout=Duration.seconds(900),
            memory_size=1024,
            layers=[pillow_layer, neptune_layer],
            vpc=vpc,
            environment={
                "BUCKET_NAME": bucket.bucket_name,
                "TABLE_NAME": table.table_name,
                "COLLECTION_ID": "faces",
                "MODEL_ID": "us.anthropic.claude-3-5-sonnet-20241022-v2:0",
                "NEPTUNE_ENDPOINT": neptune_cluster.attr_endpoint,
            },
        )

        # Search Handler Lambda
        search_handler = _lambda.Function(self, "Search",
            runtime=_lambda.Runtime.PYTHON_3_11,
            handler="search_handler.handler",
            code=_lambda.Code.from_asset("lambda"),
            role=lambda_role,
            timeout=Duration.seconds(30),
            vpc=vpc,
            layers=[neptune_layer],
            environment={
                "TABLE_NAME": table.table_name,
                "BUCKET_NAME": bucket.bucket_name,
                "NEPTUNE_ENDPOINT": neptune_cluster.attr_endpoint
            }
        )

        # Faces Handler Lambda
        faces_handler = _lambda.Function(self, "Faces",
            runtime=_lambda.Runtime.PYTHON_3_11,
            handler="faces_handler.handler",
            code=_lambda.Code.from_asset("lambda"),
            role=lambda_role,
            timeout=Duration.seconds(30),
            environment={
                "COLLECTION_ID": "faces",
                "BUCKET_NAME": bucket.bucket_name
            }
        )

        # Style Caption Lambda
        style_caption = _lambda.Function(self, "StyleCaption",
            runtime=_lambda.Runtime.PYTHON_3_11,
            handler="style_caption.handler",
            code=_lambda.Code.from_asset("lambda"),
            role=lambda_role,
            timeout=Duration.seconds(60),
            layers=[pillow_layer],
            environment={
                "TABLE_NAME": table.table_name,
                "MODEL_ID": "us.anthropic.claude-3-5-sonnet-20241022-v2:0",
            }
        )
        
        # Relationships Handler Lambda (Neptune version)
        relationships_handler = _lambda.Function(self, "RelationshipsHandler",
            runtime=_lambda.Runtime.PYTHON_3_11,
            handler="relationships_handler_neptune.handler",
            code=_lambda.Code.from_asset("lambda"),
            role=lambda_role,
            timeout=Duration.seconds(60),
            vpc=vpc,
            layers=[neptune_layer],
            environment={
                "NEPTUNE_ENDPOINT": neptune_cluster.attr_endpoint,
            }
        )
        
        # Label Relationships Handler Lambda
        label_relationships_handler = _lambda.Function(self, "LabelRelationshipsHandler",
            runtime=_lambda.Runtime.PYTHON_3_11,
            handler="label_relationships.handler",
            code=_lambda.Code.from_asset("lambda"),
            role=lambda_role,
            timeout=Duration.seconds(60),
            vpc=vpc,
            layers=[neptune_layer],
            environment={
                "NEPTUNE_ENDPOINT": neptune_cluster.attr_endpoint,
            }
        )

        # Cognito User Pool
        user_pool = cognito.UserPool(self, "UserPool",
            user_pool_name="ImageRecognitionUsers",
            sign_in_aliases=cognito.SignInAliases(email=True),
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            password_policy=cognito.PasswordPolicy(
                min_length=8,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True
            )
        )
        
        # User Pool Client
        user_pool_client = user_pool.add_client("UserPoolClient",
            user_pool_client_name="ImageRecognitionClient",
            generate_secret=False,
            auth_flows=cognito.AuthFlow(
                user_password=True,
                user_srp=True
            )
        )
        
        # API Gateway with Cognito authentication
        api = apigateway.RestApi(self, "Api",
            rest_api_name="Image Recognition API",
            default_cors_preflight_options=apigateway.CorsOptions(
                allow_origins=apigateway.Cors.ALL_ORIGINS,
                allow_methods=apigateway.Cors.ALL_METHODS,
                allow_headers=["*"]
            )
        )
        
        # Cognito Authorizer
        authorizer = apigateway.CognitoUserPoolsAuthorizer(self, "Authorizer",
            cognito_user_pools=[user_pool]
        )

        # API Routes with Cognito authentication
        faces = api.root.add_resource("faces")
        faces.add_method("POST", apigateway.LambdaIntegration(face_indexer), authorizer=authorizer)

        images = api.root.add_resource("images")
        images.add_method("POST", apigateway.LambdaIntegration(image_processor), authorizer=authorizer)

        search = api.root.add_resource("search")
        search.add_method("GET", apigateway.LambdaIntegration(search_handler), authorizer=authorizer)

        faces_list = api.root.add_resource("faces-list")
        faces_list.add_method("GET", apigateway.LambdaIntegration(faces_handler), authorizer=authorizer)
        
        style = api.root.add_resource("style-caption")
        style.add_method("POST", apigateway.LambdaIntegration(style_caption), authorizer=authorizer)
        
        relationships = api.root.add_resource("relationships")
        relationships.add_method("GET", apigateway.LambdaIntegration(relationships_handler), authorizer=authorizer)
        relationships.add_method("POST", apigateway.LambdaIntegration(relationships_handler), authorizer=authorizer)
        
        label_relationships = api.root.add_resource("label-relationships")
        label_relationships.add_method("GET", apigateway.LambdaIntegration(label_relationships_handler), authorizer=authorizer)
        


        # S3 Invoke Permissions
        image_processor.add_permission(
            "S3InvokePermission",
            principal=iam.ServicePrincipal("s3.amazonaws.com"),
            action="lambda:InvokeFunction",
            source_arn=bucket.bucket_arn
        )

        face_indexer.add_permission(
            "S3InvokePermissionFaces",
            principal=iam.ServicePrincipal("s3.amazonaws.com"),
            action="lambda:InvokeFunction",
            source_arn=bucket.bucket_arn
        )

        # S3 Triggers
        bucket.add_event_notification(
            s3.EventType.OBJECT_CREATED,
            s3n.LambdaDestination(image_processor),
            s3.NotificationKeyFilter(prefix="images/")
        )

        bucket.add_event_notification(
            s3.EventType.OBJECT_CREATED,
            s3n.LambdaDestination(face_indexer),
            s3.NotificationKeyFilter(prefix="faces/")
        )

        # Outputs
        from aws_cdk import CfnOutput
        CfnOutput(self, "BucketOut", value=bucket.bucket_name)
        CfnOutput(self, "ApiEndpoint", value=api.url)
        CfnOutput(self, "NeptuneEndpoint", value=neptune_cluster.attr_endpoint)
        CfnOutput(self, "UserPoolId", value=user_pool.user_pool_id)
        CfnOutput(self, "UserPoolClientId", value=user_pool_client.user_pool_client_id)